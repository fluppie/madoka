

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

from homeassistant.components.bluetooth import async_ble_device_from_address
from multiprocessing import Process, Queue
from multiprocessing.managers import BaseManager
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from . import pairing_issue
from . import protocol
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

    def add_known(self, addr):
        self.known.add(addr)

    def mark_connected(self, addr):
        self.connected.add(addr)

    def mark_disconnected(self, addr):
        self.connected.discard(addr)

    @property
    def disconnected(self):
        return self.known - self.connected


_registry = _ConnectionRegistry()


class Madoka:
    def __init__(self, name, addr, connect_cb=None, hass=None):
        logger.info(f'Madoka __init__ {name} {addr}')
        self.name = name
        self.addr = addr
        self.connect_cb = connect_cb
        self.hass = hass
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
        remaining = _registry.disconnected
        dConnected = time.time() - self.connectTime
        dConnectStr = str(datetime.timedelta(seconds = int(dConnected)))

        if len(remaining) > 1:
            log_warning(logger, f'{client.address} disconnected. was connected for {dConnectStr}. remaining: {remaining}', self)
        else:
            log_warning(logger, f'{client.address} disconnected. was connected for {dConnectStr}', self)
        self.disconnectTime = time.time()
        self.client = None
        # await self.connect_cb(False)


    def notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        self.framer.feed_chunk(data)


    def _note_pairing_failure(self):
        self._pairing_failures += 1
        if self._pairing_failures >= PAIRING_FAILURE_THRESHOLD and not self._pairing_issue_active:
            self._pairing_issue_active = True
            log_warning(logger, f"{self._pairing_failures} consecutive pairing failures - reporting pairing_required issue", self)
            pairing_issue.report_pairing_required(self.hass, self.addr, self.name)


    def _note_pairing_success(self):
        self._pairing_failures = 0
        if self._pairing_issue_active:
            self._pairing_issue_active = False
            pairing_issue.clear_pairing_required(self.hass, self.addr, self.name)


    async def _connect(self):
        try:

            device = async_ble_device_from_address(self.hass, self.addr)

            log_info(logger, f'about to connect {self.addr} dev:{device}', self)

            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                disconnected_callback = self.handle_disconnect,
                max_attempts=1,
                timeout = 3,
            )

            log_info(logger, f'client_connect complete {self.addr}', self)
            # await client.set_connection_params(
            #     min_interval=80,    # 100ms
            #     max_interval=80,    # 100ms
            #     latency=0,
            #     timeout=300,
            #     )

        except BleakNotFoundError as err:
            log_error(logger, f'connect BleakNotFoundError {self.addr}', self)
            return None

        except TimeoutError:
            log_error(logger, f'connect TimeoutError {self.addr}', self)
            return None

        except Exception:
            # logger.error(f'connect failed exception {self.name} {self.addr}')
            log_exception(logger, f'connect failed exception {self.addr}', self)
            return None

        if (not client.is_connected):
            log_info(logger, f'unexpected disconnect state {self.addr}', self)
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
            log_debug(logger, f'pairing {self.addr}', self)
            await asyncio.wait_for(client.pair(), timeout=15)
            log_info(logger, f'paired {self.addr}', self)
        except asyncio.TimeoutError:
            log_warning(logger, f'pair() timed out {self.addr}', self)
        except Exception as e:
            log_warning(logger, f'pair() failed or not required {self.addr}: {type(e).__name__}: {e}', self)

        try:
            log_debug(logger, f'about to start_notify {self.addr}', self)

            if (not client.is_connected):
                log_info(logger, f'unexpected disconnect state {self.addr}', self)
                self._note_pairing_failure()
                return None

            self.char = client.services.get_characteristic(NOTIFY_CHAR_UUID)
            log_debug(logger, f'found char {self.char}. uuid: {self.char.uuid} handle: {self.char.handle} properties: {self.char.properties}', self)

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

            log_info(logger, "notification registered", self)
        except Exception:
            log_error(logger, f'start_notify failed {client.is_connected} {self.addr}', self)
            await client.disconnect()
            self._note_pairing_failure()
            return None

        if (not client.is_connected):
            log_warning(logger, "connection not successful", self)
            self._note_pairing_failure()
            return None

        self._note_pairing_success()
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
        remaining = _registry.disconnected

        dDisconnected = time.time() - self.disconnectTime

        dConnected = time.time() - self.connectTime
        dConnectStr = str(datetime.timedelta(seconds=int(dConnected)))
        self.connectTime = time.time()

        if len(remaining):
            log_warning(logger, f"{self.addr} reconnected after {dDisconnected:.1f}s. was connected for {dConnectStr} remaining:{remaining}", self)
        else:
            log_warning(logger, f"{self.addr} reconnected after {dDisconnected:.1f}s was connected for {dConnectStr}", self)
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
