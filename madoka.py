

import sys
import os
import asyncio
import logging
import argparse
import time
import datetime

from logging.handlers import RotatingFileHandler
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import BleakNotFoundError
from bleak.exc import BleakError

from homeassistant.components.bluetooth import async_ble_device_from_address, async_scanner_devices_by_address
from homeassistant.helpers import device_registry as dr
from multiprocessing import Process, Queue
from multiprocessing.managers import BaseManager
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from . import pairing_issue
from . import protocol
from .const import DEFAULT_PAIRING_TIMEOUT
from .framer import Framer
from .helpers import format_hex, log_context, TRACE_LEVEL, log_debug, log_trace, log_info, log_warning, log_error, log_exception

logger = logging.getLogger(__name__)
logger.propagate = True  # don't send to root
# logger.setLevel(TRACE_LEVEL)

SERVICE_UUID     = "2141E110-213A-11E6-B67B-9E71128CAE77"
NOTIFY_CHAR_UUID = "2141E111-213A-11E6-B67B-9E71128CAE77"
WRITE_CHAR_UUID  = "2141E112-213A-11E6-B67B-9E71128CAE77"

# consecutive pairing-stage failures (connect succeeded but pair()/
# start_notify didn't) before we surface a Repair Issue - high enough to
# ride out the occasional single-attempt hiccup seen in testing (which
# self-heals on the very next retry) without bothering the user over it
PAIRING_FAILURE_THRESHOLD = 3

# consecutive establish_connection() failures (i.e. before pair()/
# start_notify even runs) on an already-bonded device before we assume the
# local bond is stale and remove it ourselves. Seen in practice when the
# peripheral's own bond is cleared (factory reset, manual unpair) while a
# local controller still holds the old key: every encryption attempt is then
# silently rejected and the link drops before service discovery completes,
# forever, with no way to recover short of removing the stale key so the
# next attempt triggers a genuine fresh pairing instead.
CONNECT_FAILURE_THRESHOLD = 3

# how long a device may hold up the config-order bootstrap turn (see
# _ConnectionRegistry.bootstrap_turn) before it's forced to yield to the
# next device anyway - so one unreachable/offline unit can delay, but never
# permanently block, every device configured after it from ever getting
# their own turn to bootstrap.
BOOTSTRAP_TURN_TIMEOUT = 90


async def _run_bluetoothctl(*args, timeout=10):
    """Run a single non-interactive bluetoothctl subcommand and return its
    combined stdout/stderr. Used instead of talking BlueZ D-Bus directly
    since this mirrors exactly the manual recovery steps already verified
    to work for this problem."""
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", "--", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return stdout.decode(errors="replace")


async def _local_controllers():
    """Return the set of local BlueZ controller addresses, as reported by
    `bluetoothctl list` - i.e. this host's own adapters, never a remote
    source like an ESPHome Bluetooth proxy (those don't share bonding with
    the host's BlueZ stack at all, so there's nothing for us to manage
    there)."""
    out = await _run_bluetoothctl("list")
    return {line.split()[1] for line in out.splitlines() if line.strip().startswith("Controller")}


def _source_name(hass, source_address):
    """Resolve a BLE adapter/proxy's address (as reported by HA's bluetooth
    integration in BLEDevice.details["source"]) to its name in HA's device
    registry - whatever it's actually called/renamed to there - for the
    connect log. Falls back to the raw address if hass isn't available yet
    or the device isn't found in the registry."""
    if not hass or not source_address:
        return source_address

    registry = dr.async_get(hass)
    device = registry.async_get_device(connections={(dr.CONNECTION_BLUETOOTH, source_address)})
    if device is None:
        return source_address

    return device.name_by_user or device.name or source_address

class _ConnectionRegistry:
    """State shared across every Madoka instance (i.e. every configured
    device), as opposed to the per-device state that lives on Madoka
    instances themselves: which addresses exist, which are currently
    connected, and a semaphore serializing BLE connect attempts so two
    devices don't try to connect at the same time."""

    def __init__(self):
        self.known = set()
        self.connected = set()
        self.connect_sem = asyncio.Semaphore(1)

        # how many of our own devices are currently connected via each local
        # controller address - tracked ourselves rather than trusting
        # bluetoothctl's own "Connected" reporting, which has been observed
        # to report the same devices as connected on multiple controllers at
        # once on this system (stale per-controller D-Bus state). Used to
        # spread bootstrap pairing across controllers instead of piling
        # devices onto whichever one is found first and hitting its
        # connection-slot limit (typically ~5) while others sit idle.
        self.controller_load = {}

        # whose turn it is to run bootstrap pairing, by config-file order
        # (see Madoka._bootstrap_order) - advanced only once a device
        # finishes (or times out - see BOOTSTRAP_TURN_TIMEOUT) its own
        # bootstrap, so one device's full multi-interface bootstrap
        # completes before the next one even starts trying, instead of
        # every device's attempts interleaving via the plain connect_sem.
        self.bootstrap_turn = 0

    def advance_bootstrap_turn(self, order):
        self.bootstrap_turn = max(self.bootstrap_turn, order + 1)

    def add_known(self, addr):
        self.known.add(addr)

    def mark_connected(self, addr):
        self.connected.add(addr)

    def mark_disconnected(self, addr):
        self.connected.discard(addr)

    def mark_connected_via(self, controller):
        if controller:
            self.controller_load[controller] = self.controller_load.get(controller, 0) + 1

    def mark_disconnected_via(self, controller):
        if controller and self.controller_load.get(controller):
            self.controller_load[controller] -= 1

    @property
    def disconnected(self):
        return self.known - self.connected


_registry = _ConnectionRegistry()


class Madoka:
    def __init__(self, name, addr, connect_cb=None, hass=None, pairing_timeout=DEFAULT_PAIRING_TIMEOUT, bootstrap_order=0):
        logger.info(f'Madoka __init__ {name} {addr} pairing_timeout:{pairing_timeout} bootstrap_order:{bootstrap_order}')
        self.name = name
        self.addr = addr
        self.connect_cb = connect_cb
        self.hass = hass
        self.pairing_timeout = pairing_timeout
        # this device's position in the config file - process() won't start
        # attempting to connect/bootstrap-pair until _registry.bootstrap_turn
        # reaches this index, so devices bootstrap fully, one at a time, in
        # config-file order (see _ConnectionRegistry.bootstrap_turn).
        self._bootstrap_order = bootstrap_order
        self._bootstrap_turn_since = None  # set when this device starts waiting/holding its turn
        self._connect_source = None  # set by _connect(), read back by connect()'s reconnect log
        self._connect_source_addr = None  # set by _connect(), read back by _recover_failed_controller()

        # One-time bootstrap pairing: until every local BlueZ controller
        # that can currently see this device has had a real, verified
        # connection this run, _connect() explicitly pins each attempt to an
        # unverified-but-visible controller instead of letting HA pick
        # automatically - so every interface gets proven to actually work
        # up front (silently, if it already did - only a broken one prompts
        # for pairing), rather than risking a later automatic failover
        # landing on a controller nobody's confirmed works. Once nothing
        # visible is left unverified, this flips true and _connect() goes
        # back to plain HA automatic adapter selection.
        # _recover_failed_controller() flips it back to False for a
        # controller that turns out to need (re-)pairing after all, so that
        # one goes through the careful path again too - whether it once had
        # a now-stale bond, or was simply missed by the one-shot visibility
        # check bootstrap first ran with.
        self._bootstrap_done = False
        # local controller addresses this run has actually proven work with
        # a real successful connection - not just a "Bonded: yes" claim,
        # which has been observed lying on this system. Populated in
        # _connect() on success, cleared per-controller by
        # _recover_failed_controller() when one turns out not to.
        self._verified_controllers = set()
        self.disconnectTime = time.time()
        self.connectTime    = self.disconnectTime
        self.state = {}
        _registry.add_known(addr)

        # each item is (tx_dict, response_future); process() resolves the
        # future with that specific request's response, so concurrent
        # bluetooth_transaction() calls can never receive each other's
        # answers (there used to be a single shared rxQueue here instead).
        self.txQueue = asyncio.Queue()
        self.client = None
        self._pairing_failures = 0
        self._pairing_issue_active = False
        self._connect_failures = 0
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()
        self.framer = Framer(self._loop, owner=self)
        self._process_task = asyncio.create_task(self.process())


    async def _reset_client_state(self, client):
        self.client = None
        self.framer.reset()

        # log_debug(logger, "stopping _reset_client_state client:{client} is_connected:{getattr(client, "is_connected", False)}", self)
        log_debug(logger, "stopping _reset_client_state client:{client}", self)

        # if client is not None and getattr(client, "is_connected", False):
        if client is None:
            return

        # both calls are bounded: this method runs both as a direct call from
        # connect() (covered by process()'s outer watchdog) and as a
        # fire-and-forget task spawned from handle_disconnect() (which is
        # NOT covered by that watchdog) - an unbounded hang here on the
        # latter path can silently tie up the adapter/connection with no
        # timeout, no exception, and no log line to notice it by.
        try:
            log_info(logger, "stopping notifications char: {self.char}", self)
            await asyncio.wait_for(client.stop_notify(self.char), timeout=5)
            log_info(logger, "notifications stopped", self)
        except Exception:
            pass
        try:
            log_info(logger, "before client.disconnect", self)
            await asyncio.wait_for(client.disconnect(), timeout=5)
            log_info(logger, "after client.disconnect", self)
        except Exception:
            pass

        await self.txQueue.put(([], None))
        self.client = None


    def handle_disconnect(self, client):
        if client != self.client:
            log_warning(logger, f"handle_disconnect called with unexpected client {client.address}. expected {self.client.address if self.client else 'None'}", self)
            # return

        if self.client is None:
            log_info(logger, f'handle_disconnect while client is None. {client.address}', self)
            return

        try:
            if getattr(self._loop, "is_running", lambda: False)():
                self._loop.create_task(self._reset_client_state(client))
            else:
                asyncio.run(self._reset_client_state(client))
        except Exception:
            log_exception(logger, "handle_disconnect cleanup failed", self)

        _registry.mark_disconnected(client.address)
        _registry.mark_disconnected_via(self._connect_source_addr)
        remaining = _registry.disconnected
        dConnected = time.time() - self.connectTime
        dConnectStr = str(datetime.timedelta(seconds = int(dConnected)))

        if len(remaining) > 1:
            log_warning(logger, f'{client.address} disconnected from {self._connect_source}. was connected for {dConnectStr}. remaining: {remaining}', self)
        else:
            log_warning(logger, f'{client.address} disconnected from {self._connect_source}. was connected for {dConnectStr}', self)
        self.disconnectTime = time.time()
        self.client = None
        # await self.connect_cb(False)


    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        self.framer.feed_chunk(data)


    async def _note_pairing_failure(self):
        """Called on every pairing-stage failure (pair()/start_notify/the
        is_connected checks around them) - as distinct from
        establish_connection()-stage failures, which _connect() counts and
        recovers from separately via _connect_failures. Both can mean the
        same underlying thing (a stale bond on the peripheral's side), just
        surfacing differently: this is what showed up as pair() succeeding
        instantly (reusing a key BlueZ still has) followed by the link
        dying within milliseconds, rather than a clean encryption-stage
        rejection - so this path needs its own trigger into
        _recover_failed_controller() rather than assuming the
        establish_connection() path will always be the one to catch it."""
        self._pairing_failures += 1
        if self._pairing_failures >= PAIRING_FAILURE_THRESHOLD:
            self._pairing_failures = 0
            if not self._pairing_issue_active:
                self._pairing_issue_active = True
                log_warning(logger, f"{PAIRING_FAILURE_THRESHOLD} consecutive pairing failures - reporting pairing_required issue", self)
                pairing_issue.report_pairing_required(self.hass, self.addr, self.name)
            log_warning(logger, f"{PAIRING_FAILURE_THRESHOLD} consecutive pairing failures on {self._connect_source_addr} - recovering", self)
            await self._recover_failed_controller()


    def _note_pairing_success(self):
        self._pairing_failures = 0
        if self._pairing_issue_active:
            self._pairing_issue_active = False
            pairing_issue.clear_pairing_required(self.hass, self.addr, self.name)


    async def _recover_failed_controller(self):
        """Called after repeated connect failures on one local BlueZ
        controller. Two distinct cases end up here, and both are handled by
        re-arming bootstrap pairing for this device so the careful,
        explicit-per-interface path (not blind automatic selection) picks
        this controller up again:

        - it's bonded with a stale/mismatched key (e.g. the peripheral's own
          bond was cleared) - remove that bond so the next attempt triggers
          a genuine fresh pairing.
        - it was never bonded in the first place - this happens when
          bootstrap's one-shot "who's visible" check ran before this
          controller had a chance to see the device (e.g. right at startup,
          before its scan cache warmed up), so bootstrap already locked in
          as done without ever pairing it. Nothing to remove, but bootstrap
          still needs to run again for this controller now that it's
          reporting the device visible and failing.

        Only the one controller that actually attempted (and failed) this
        connection has its bond touched - bonds are per-controller, and the
        other configured adapters may still hold a perfectly good key of
        their own. But this device's *entire* set of previously-verified
        controllers is discarded, not just this one: a stale bond here is
        evidence the peripheral's own pairing may have been reset outright
        (e.g. the user cleared all pairing on the unit itself), which would
        equally stale every other controller's bond too, even ones that
        haven't failed yet simply because they haven't been tried again
        since. Re-verifying an already-good bond is cheap and silent (a
        near-instant pair(), no prompt) - so erring toward re-checking
        everything costs little, while erring the other way was exactly the
        gap that let a second interface's stale bond go undetected.
        """
        controller = self._connect_source_addr
        if not controller:
            log_warning(logger, "_recover_failed_controller: no local controller recorded for this attempt, skipping", self)
            return

        known = await _local_controllers()
        if controller not in known:
            # e.g. an ESPHome Bluetooth proxy address - not a local BlueZ
            # controller, so there's no bond here for us to manage.
            log_warning(logger, f"_recover_failed_controller: {controller} is not a local controller, skipping", self)
            return

        await _run_bluetoothctl("select", controller)
        info = await _run_bluetoothctl("info", self.addr)
        self._verified_controllers.clear()
        controller_name = _source_name(self.hass, controller)

        if "Bonded: yes" not in info:
            log_warning(logger, f"BS {controller_name}: isn't bonded yet - re-arming bootstrap pairing for every interface", self)
            self._bootstrap_done = False
            return

        result = await _run_bluetoothctl("remove", self.addr)

        # bluetoothctl subscribes to D-Bus property-change events even for a
        # one-shot command, so its captured stdout can be unrelated event
        # chatter instead of (or as well as) an actual removal confirmation -
        # don't just trust the command exited without checking the bond is
        # actually gone, or a silently-failed remove would leave the old key
        # in place, and a subsequent "pairing" would just silently reuse it
        # rather than fail into a real, visible renegotiation.
        recheck = await _run_bluetoothctl("info", self.addr)
        if "Bonded: yes" in recheck:
            log_error(logger, f"BS {controller_name}: bond removal did not take effect ({result.strip()}) - still bonded", self)
        else:
            log_warning(logger, f"BS {controller_name}: removed stale bond: {result.strip()} - re-arming bootstrap pairing for every interface", self)
        # this controller (and possibly others, per above) now genuinely
        # needs pairing again - go through the careful bootstrap path
        # rather than letting the very next attempt fall back to plain
        # automatic adapter selection.
        self._bootstrap_done = False


    async def _select_bootstrap_device(self):
        """One-time bootstrap pairing helper for _connect(). Finds a local
        BlueZ controller that currently sees this device but hasn't actually
        been proven to work THIS RUN, and returns its BLEDevice so the
        caller pins this attempt to that specific interface instead of HA's
        automatic choice - so every interface gets a real, verified
        connection deliberately, once, rather than risking HA later
        auto-failing-over to a controller nobody's actually confirmed works.

        Deliberately does not treat BlueZ's own "Bonded: yes" as proof of
        that - it's been observed reporting bonded (and even connected) on
        controllers with no working key at all on this system, so the only
        thing trusted here is a connection this code itself just completed
        (self._verified_controllers, populated in _connect() on success).
        An already-genuinely-bonded controller still gets pinned once, but
        pair() on it completes silently/instantly with no on-unit prompt -
        only an actually-broken bond surfaces one, which is the point.

        Returns None either to fall back to HA's normal automatic selection
        for this one attempt (nothing visible yet to bootstrap against), or
        because every local controller currently visible has already been
        verified - in which case self._bootstrap_done is also set True so
        future calls skip this check entirely.
        """
        known = await _local_controllers()
        if not known:
            return None

        try:
            scanner_devices = async_scanner_devices_by_address(self.hass, self.addr, connectable=True)
        except Exception:
            log_exception(logger, "_select_bootstrap_device: scanner lookup failed", self)
            return None

        visible = {d.scanner.source: d.ble_device for d in scanner_devices if d.scanner.source in known}
        if not visible:
            return None

        unverified = {hci: dev for hci, dev in visible.items() if hci not in self._verified_controllers}
        if not unverified:
            self._bootstrap_done = True
            _registry.advance_bootstrap_turn(self._bootstrap_order)
            verified_names = [_source_name(self.hass, hci) for hci in (self._verified_controllers & known)]
            log_info(logger, f"BS: complete - verified on: {verified_names}. switching to automatic adapter selection", self)
            return None

        hci, ble_device = min(unverified.items(), key=lambda item: _registry.controller_load.get(item[0], 0))
        hci_name = _source_name(self.hass, hci)
        log_warning(logger, f"BS {hci_name}: verifying (load {_registry.controller_load.get(hci, 0)}), pinning this attempt to it", self)
        # returned alongside the BLEDevice, not re-derived from it - a
        # BLEDevice obtained this way (vs. HA's own
        # async_ble_device_from_address()) has been observed to not carry
        # details["source"] at all, which would otherwise silently break
        # verification bookkeeping (self._connect_source_addr ending up
        # None) and cause bootstrap to loop on this controller forever.
        return hci, ble_device


    async def _connect(self):
        bs = ""  # set to "BS " below once we know this is a bootstrap-pinned
                 # attempt - defined here too so the except blocks below can
                 # always reference it, even if something raises before that
        try:

            device = None
            is_bootstrap_attempt = False
            bootstrap_source = None
            if not self._bootstrap_done:
                result = await self._select_bootstrap_device()
                if result is not None:
                    bootstrap_source, device = result
                    is_bootstrap_attempt = True
            if device is None:
                device = async_ble_device_from_address(self.hass, self.addr)

            if is_bootstrap_attempt:
                # known directly from the selection above - a BLEDevice
                # obtained via async_scanner_devices_by_address() (bootstrap's
                # path) doesn't reliably carry details["source"] the way one
                # from async_ble_device_from_address() does.
                source = bootstrap_source
            else:
                # .details["source"] is the adapter/proxy HA picked to service
                # this device (e.g. the internal adapter's, a USB dongle's, or
                # an ESPHome Bluetooth proxy's own address) - logged so a flaky
                # connection can be traced back to a specific source instead of
                # guessing which of several configured adapters was involved.
                source = getattr(device, "details", None)
                source = source.get("source") if isinstance(source, dict) else None
            source_name = _source_name(self.hass, source)
            self._connect_source = source_name
            self._connect_source_addr = source
            bs = f"BS {source_name}: " if is_bootstrap_attempt else ""

            log_info(logger, f'{bs}about to connect dev:{device} source:{source_name}', self)

            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                disconnected_callback = self.handle_disconnect,
                max_attempts=1,
                timeout = 3,
            )

            log_info(logger, f'{bs}client_connect complete {self.addr}', self)
            self._connect_failures = 0
            # await client.set_connection_params(
            #     min_interval=80,    # 100ms
            #     max_interval=80,    # 100ms
            #     latency=0,
            #     timeout=300,
            #     )

        except BleakNotFoundError as err:
            log_error(logger, f'{bs}connect BleakNotFoundError {self.addr}', self)
            return None

        except TimeoutError:
            log_error(logger, f'{bs}connect TimeoutError {self.addr}', self)
            return None

        except Exception:
            # logger.error(f'connect failed exception {self.name} {self.addr}')
            log_exception(logger, f'{bs}connect failed exception {self.addr}', self)
            self._connect_failures += 1
            if self._connect_failures >= CONNECT_FAILURE_THRESHOLD:
                self._connect_failures = 0
                log_warning(logger, f"{CONNECT_FAILURE_THRESHOLD} consecutive connect failures on {self._connect_source_addr} - recovering", self)
                await self._recover_failed_controller()
            return None

        if (not client.is_connected):
            log_info(logger, f'{bs}unexpected disconnect state {self.addr}', self)
            return None

        # From here on the link itself is up, so any further failure below
        # is specifically a pairing/authentication problem rather than the
        # device being unreachable - tracked separately via
        # _note_pairing_failure()/_note_pairing_success() so a Repair Issue
        # can point the user at the one failure mode where only they can
        # fix it (confirming a prompt on the physical unit). See
        # pairing_issue.py.

        # Explicit pairing: reactive pairing (relying on start_notify() to
        # trigger it implicitly) was tried and does not happen at all on
        # this system - start_notify just fails outright, with no pairing
        # attempted on either side. An earlier attempt at calling pair()
        # explicitly here also failed (near-instant disconnect), but that
        # was with a NoInputNoOutput agent forcing Just Works, which this
        # device appears to reject; now using DisplayYesNo (see
        # pairing_agent.py) so the device gets its normal Numeric
        # Comparison method instead. Bounded so a stuck negotiation can't
        # hang forever and non-fatal so an already-paired device (where
        # this just fails harmlessly) doesn't get blocked from connecting.
        try:
            log_debug(logger, f'{bs}pairing {self.addr}', self)
            await asyncio.wait_for(client.pair(), timeout=self.pairing_timeout)
            log_info(logger, f'{bs}paired {self.addr}', self)
        except asyncio.TimeoutError:
            # our wait_for gave up, but the underlying D-Bus Pair() call
            # already dispatched to bluetoothd keeps running regardless -
            # proceeding straight to start_notify() would race a pairing
            # negotiation that may still be in flight. Force a disconnect to
            # clear it and retry fresh next cycle instead of racing it.
            log_warning(logger, f'{bs}pair() timed out {self.addr}, disconnecting to clear in-flight negotiation', self)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception:
                pass
            await self._note_pairing_failure()
            return None
        except Exception as e:
            log_warning(logger, f'{bs}pair() failed or not required {self.addr}: {type(e).__name__}: {e}', self)

        try:
            log_debug(logger, f'{bs}about to start_notify {self.addr}', self)

            if (not client.is_connected):
                log_info(logger, f'{bs}unexpected disconnect state {self.addr}', self)
                await self._note_pairing_failure()
                return None

            self.char = client.services.get_characteristic(NOTIFY_CHAR_UUID)
            log_debug(logger, f'{bs}found char {self.char}. uuid: {self.char.uuid} handle: {self.char.handle} properties: {self.char.properties}', self)

            # start_notify can implicitly trigger BlueZ's reactive pairing
            # negotiation if the characteristic requires it - bounded so a
            # stuck negotiation can't hang forever. This matters a lot more
            # than a typical timeout: connect() runs under a single global
            # semaphore shared by every configured device (see process()),
            # so an unbounded hang here doesn't just break this device, it
            # starves every other device from ever getting a turn to connect.
            await asyncio.wait_for(
                client.start_notify(
                    self.char,
                    self.notification_handler,
                    bluez={"use_start_notify": True},
                ),
                timeout=10,
            )

            log_info(logger, f"{bs}notification registered", self)
        except Exception:
            log_error(logger, f'{bs}start_notify failed {client.is_connected} {self.addr}', self)
            await client.disconnect()
            await self._note_pairing_failure()
            return None

        if (not client.is_connected):
            log_warning(logger, f"{bs}connection not successful", self)
            await self._note_pairing_failure()
            return None

        # is_connected read immediately after start_notify() isn't reliable -
        # observed flip-flopping true/false between two attempts on this
        # device seconds apart. Debounce with a short recheck before
        # trusting it and declaring the connection good.
        await asyncio.sleep(1)
        if (not client.is_connected):
            log_warning(logger, f"{bs}connection dropped shortly after connecting", self)
            await self._note_pairing_failure()
            return None

        self._note_pairing_success()
        self._verified_controllers.add(self._connect_source_addr)

        if is_bootstrap_attempt:
            # keep cycling through bootstrap pairing, one interface at a
            # time, instead of settling into this connection - re-checking
            # here also naturally re-uses _select_bootstrap_device()'s own
            # "nothing left unverified" detection, which flips
            # self._bootstrap_done for us when this was the last one.
            still_pending = await self._select_bootstrap_device()
            if still_pending is not None:
                log_info(logger, f"BS {self._connect_source}: verified, disconnecting to continue with the next unverified interface", self)
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5)
                except Exception:
                    pass
                return None

        return client


    async def connect(self):
        log_debug(logger, f'connect {self.addr}', self)
        try:
            if self.client is not None:
                if (self.client.is_connected):
                    log_info(logger, f'already connected {self.addr}', self)
                    return True

        except NameError:
            log_debug(logger, f"client does not exist {self.addr}", self)

        except Exception:
            log_exception(logger, "unexpected error checking client connection", self)

        # logger.info(f'{self.addr} ENTER >>>>>>>>>>>>>>>>>>>>>>>>>')
        log_debug(logger, f'Looking for Bluetooth LE device at address `{self.addr}`...', self)

        await self._reset_client_state(self.client)
        self.client = None

        client = await self._connect()
        # logger.info(f'{self.addr} EXIT <<<<<<<<<<<<<<<<<<<<<<<<<')

        if client is None:
            #logger.warning(f'failed to connect. waiting 5 sec')
            #await asyncio.sleep(5)
            self.client = None
            return False

        self.client = client
        log_debug(logger, f'client_connect {self.addr} connected:{client.is_connected}', self)
    
        _registry.mark_connected(self.addr)
        _registry.mark_connected_via(self._connect_source_addr)
        remaining = _registry.disconnected

        dDisconnected = time.time() - self.disconnectTime

        dConnected = time.time() - self.connectTime
        dConnectStr = str(datetime.timedelta(seconds=int(dConnected)))
        self.connectTime = time.time()

        if len(remaining):
            log_warning(logger, f"{self.addr} reconnected after {dDisconnected:.1f}s via {self._connect_source}. was connected for {dConnectStr} remaining:{remaining}", self)
        else:
            log_warning(logger, f"{self.addr} reconnected after {dDisconnected:.1f}s via {self._connect_source} was connected for {dConnectStr}", self)
        self.connectedTime = time.time()
        if callable(self.connect_cb):
            await self.connect_cb(True)

        return True


    async def send(self, chunks):
        if self.client is None:
            log_error(logger, f'send {self.addr} client is None', self)
            return

        for idx, chunk in enumerate(chunks):
            log_debug(logger, f"sending chunk: ({len(chunk)}){format_hex(chunk)}", self)
            try:
                await asyncio.wait_for(self.client.write_gatt_char(WRITE_CHAR_UUID, chunk), timeout=5)
            except Exception:
                log_exception(logger, f"send: write_gatt_char failed on chunk {idx}", self)
                raise


    async def sendAndReceive(self, cmd:int, chunks):
        if self.client is None:
            log_error(logger, f'sendAndReceive {self.addr} client is None', self)
            raise Exception("client is None")

        # clear any pending response before sending a new command
        deleted = self.framer.drain()
        if deleted > 0:
            log_warning(logger, f"sendAndReceive cleared {deleted} pending notifications before sending command", self)

        try:
            await self.send(chunks)
            log_trace(logger, f'sendAndReceive sent. waiting for receive', self)
            rsp = await self.framer.wait_for(cmd)

        except Exception:
            log_error(logger, f'sendAndReceive failed', self)
            raise

        return rsp


    async def process(self):
        log_info(logger, f'startign process {self.addr}', self)
        async with _registry.connect_sem:
            log_info(logger, 'force disconnect', self)

        while True:
            try:
                await asyncio.sleep(2)

                if not self._bootstrap_done and _registry.bootstrap_turn < self._bootstrap_order:
                    # an earlier device (by config order) hasn't finished its
                    # own bootstrap pairing yet - wait, unless it's been
                    # stuck long enough that it's not worth blocking on.
                    if self._bootstrap_turn_since is None:
                        self._bootstrap_turn_since = time.time()
                    elif time.time() - self._bootstrap_turn_since > BOOTSTRAP_TURN_TIMEOUT:
                        log_warning(logger, f"BS: waited {BOOTSTRAP_TURN_TIMEOUT}s for bootstrap turn {_registry.bootstrap_turn} - proceeding out of order", self)
                    else:
                        continue

                # outer watchdog: connect_sem is a single global lock shared
                # by every configured device, so a hang anywhere inside
                # connect() - known or not yet discovered - would otherwise
                # starve every other device from ever getting a turn to
                # connect, not just this one. Set comfortably above the sum
                # of connect()'s own inner timeouts (establish_connection +
                # pair() + start_notify) so this doesn't cut them off first.
                async with _registry.connect_sem:
                    connected = await asyncio.wait_for(self.connect(), timeout=40)

                if (not connected):
                    continue

            except asyncio.TimeoutError:
                log_error(logger, "connect() watchdog timeout - releasing connect_sem for other devices", self)
                continue

            except Exception:
                log_exception(logger, "process connection exception", self)
                continue


            if self.client is None:
                log_error(logger, "unexpected client None", self)
                continue

            counter = 0
            while True:
                if self.client is None:
                    break

                if not self.client.is_connected:
                    break

                try:
                    log_trace(logger, f"waiting for txQue", self)
                    frames, response_future = await asyncio.wait_for(self.txQueue.get(), timeout=120)
                    log_debug(logger, f"tx {format_hex(frames)}", self)

                    state = {}
                    for cmd, chunks in frames:
                        log_trace(logger, f"sending cmd:{cmd:x}", self)
                        rsp = await self.sendAndReceive(cmd, chunks)
                        if rsp is None:
                            log_warning(logger, f"sendAndReceive returned None for cmd:{cmd:x}", self)
                            continue

                        log_trace(logger, f"received cmd:{cmd:x} rsp:{format_hex(rsp)}", self)
                        state.update(rsp)

                    log_trace(logger, f"state: {format_hex(state)}", self)
                    if response_future is not None and not response_future.done():
                        response_future.set_result(state)

                except asyncio.TimeoutError:
                    #logger.debug(f"txQue timeout")
                    pass

                except asyncio.CancelledError:
                    log_info(logger, f"asyncio.CancelledError", self)
                    break                

                except Exception:
                    log_exception(logger, "process data exception", self)
                    # A GATT-level failure here (e.g. write_gatt_char) means
                    # the link is actually dead even when bleak's
                    # is_connected keeps reporting True with no
                    # disconnected_callback ever firing to correct it -
                    # without this, the loop just keeps retrying against a
                    # zombie client forever instead of reconnecting.
                    if self.client is not None:
                        self.handle_disconnect(self.client)
                    break

                #await asyncio.sleep(5)
                counter += 1

        log_error(logger, f"process exited !!! {self.addr}", self)


    async def bluetooth_transaction(self, d, keyOffset=0):
        log_debug(logger, f"bluetooth_transaction: d:{format_hex(d)}", self)
        frames = protocol.encode(d, keyOffset, log_context(self))
        log_debug(logger, f"txQueue.put: {format_hex(frames)}", self)

        # a private future for this specific request - process() resolves
        # this one, so we can never end up with a stale/unrelated response
        # meant for some other concurrent bluetooth_transaction() call
        response_future = self._loop.create_future()
        await self.txQueue.put((frames, response_future))

        state = {}
        try:
            state = await asyncio.wait_for(response_future, timeout=3)

        except asyncio.TimeoutError:
            log_warning(logger, "rxQue timeout", self)
            response_future.cancel()

        except Exception:
            log_exception(logger, "bluetooth_transaction response exception", self)

        log_debug(logger, f"bluetooth_transaction state: {format_hex(state)}", self)
        return state


    async def device_command(self, cmd):
        if not hasattr(self, "client"):
            log_warning(logger, f"device {self.addr} has no BLE client", self)
            return ({"error":"not connected"})

        if (self.client == None):
            log_warning(logger, 'self.client is None', self)
            return ({"error":"not connected"})

        if not self.client.is_connected:
            log_warning(logger, "client is not connected", self)
            return ({"error":"not connected"})

        read  = cmd.get("read")
        write = cmd.get("write")

        if write is not None:
            log_debug(logger, f"calling write sequence {format_hex(write)}", self)
            stateWr = await self.bluetooth_transaction(write, 0x4000)
            log_info(logger, f"write state: {stateWr}", self)
            state = stateWr

        if read is not None:
            log_debug(logger, f"calling read sequence {format_hex(read)}", self)
            stateRd = await self.bluetooth_transaction(read)

            log_trace(logger, f"read state: {stateRd}", self)
            state = stateRd

        log_debug(logger, f"msg_send: sending responce: {state}", self)
        return state


    async def close(self):
        log_info(logger, "closing", self)
        self._process_task.cancel()
        try:
            await self._process_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log_exception(logger, "close: process task raised on cancel", self)

        await self._reset_client_state(self.client)


if __name__ == "__main__":
    print(dir(BleakClient))
    pass
