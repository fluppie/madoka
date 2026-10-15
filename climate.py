from __future__ import annotations
import asyncio
import logging
import subprocess
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACMode,
    ClimateEntityFeature,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
)

import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, ALLOWED_MODES, DEFAULT_MODES, DEFAULT_PAIRING_TIMEOUT, signal_update, device_info
from .madoka import Madoka
from .helpers import format_hex, log_context, log_debug, log_trace, log_info, log_warning, log_error, log_exception

logger = logging.getLogger(__name__)
logger.propagate = True

CONF_NAME    = "name"
CONF_MAC     = "mac"
CONF_MODES   = "modes"
CONF_PAIRING_TIMEOUT = "pairing_timeout"

MAX_OFFLINE_COUNT = 3

# the climate entity is the one HA polls (async_update); extra, slow-changing
# data for the sensor/binary_sensor platforms is read from the same
# connection every N-th poll and pushed to those entities via the
# dispatcher, so there's only ever one poller per device
SLOW_POLL_EVERY = 10      # energy / operation hours: every 10th update (~10 min)
INFO_POLL_EVERY = 120     # model / firmware info: every 120th update
UNSUPPORTED_AFTER = 2     # give up on a read after this many empty responses

# fields read every cycle, in one round-trip with the climate state
FAST_READ = {"power":0, "mode":0, "temp_indoor":0, "temp_outdoor":0, "cooling":0, "heating":0,
             "cool_fan":0, "heat_fan":0, "clean_filter":0, "notif_general":0, "notif_special":0,
             "notif_onoff":0, "notif_errors":0, "notif_schedule":0, "error_code":0}

# slow reads, each its own round-trip (energy responses are multi-chunk and
# too large to combine into one frame)
SLOW_READS = {
    "energy_today":     {"energy_today":0},
    "energy_yesterday": {"energy_yesterday":0},
    "energy_this_week": {"energy_this_week":0},
    "energy_this_year": {"energy_this_year":0},
    "hours":            {"unit_number":0, "hours_indoor":0, "hours_indoor_fan":0, "hours_indoor_power":0,
                         "hours_outdoor":0, "hours_outdoor_fan1":0, "hours_compressor1":0, "hours_compressor2":0},
}
INFO_READ = {"info_unit_number":0, "model_info":0, "fw_version":0, "ble_version":0}

# per-device-mode field names, and the HVACMode <-> device-mode-string mapping.
# Centralized here since both were previously duplicated across several
# match statements / inline dicts.
MODE_FIELDS = {
    "cool": {"temp": "cooling", "fan": "cool_fan"},
    "heat": {"temp": "heating", "fan": "heat_fan"},
}
HVAC_MODE_TO_MODE = {HVACMode.COOL: "cool", HVACMode.HEAT: "heat"}
MODE_TO_HVAC_MODE = {v: k for k, v in HVAC_MODE_TO_MODE.items()}

rq = None
rs = None
process = None


async def async_setup_entry(hass, entry, async_add_entities):
    logger.info(f'async_setup_entry: {entry.data} options:{entry.options}')
    # options (set via the options flow) take precedence over the original
    # data the entry was created with, so later edits actually apply
    config = entry.options or entry.data
    entry_devices = config.get("devices", [])

    devices = []
    macList = []

    for idx, dev in enumerate(entry_devices):
        name = dev.get(CONF_NAME)
        mac = dev.get(CONF_MAC)
        modes = dev.get(CONF_MODES, DEFAULT_MODES)
        if not isinstance(modes, list) or "off" not in modes or any(m not in ALLOWED_MODES for m in modes):
            log_warning(logger, f'{name}: invalid modes {modes!r}, falling back to {DEFAULT_MODES}')
            modes = DEFAULT_MODES

        pairing_timeout = dev.get(CONF_PAIRING_TIMEOUT, DEFAULT_PAIRING_TIMEOUT)
        if isinstance(pairing_timeout, bool) or not isinstance(pairing_timeout, (int, float)) or pairing_timeout <= 0:
            log_warning(logger, f'{name}: invalid pairing_timeout {pairing_timeout!r}, falling back to {DEFAULT_PAIRING_TIMEOUT}')
            pairing_timeout = DEFAULT_PAIRING_TIMEOUT
        #logger.info(f'name: {name}, mac:{mac}')

        myac = MyAC(name, mac, modes, hass, pairing_timeout=pairing_timeout, bootstrap_order=idx)
        devices.append(myac)
        macList.append(mac)

    async_add_entities(devices)

class MyAC(ClimateEntity):
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE |
        ClimateEntityFeature.FAN_MODE |
        ClimateEntityFeature.TURN_ON |
        ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.AUTO]
    _attr_fan_modes = [FAN_LOW, FAN_MEDIUM, FAN_HIGH]

    def __init__(self, name: str, mac: str, modes, hass=None, pairing_timeout=DEFAULT_PAIRING_TIMEOUT, bootstrap_order=0):
        log_info(logger, f'MyAC __init__ {name} {mac} modes:{modes} pairing_timeout:{pairing_timeout} bootstrap_order:{bootstrap_order}', self)

        self._madoka = Madoka(
            name,
            mac,
            connect_cb = self.madoka_connect_cb,
            hass = hass,
            pairing_timeout = pairing_timeout,
            bootstrap_order = bootstrap_order,
        )

        self._hass = hass
        self._attr_name = name
        self._mac = mac
        self._modes = modes
        self._power_only = ("cool" not in modes) and ("heat" not in modes)
        self._attr_unique_id = f"my_ac_{mac}"
        self._offline_count = MAX_OFFLINE_COUNT
        self._attr_extra_state_attributes = {}
        self._filter_needs_clean = False
        self._mode = ""
        self._pending_write = {}
        self._attr_device_info = device_info(mac, name)
        self._poll_count = 0
        self._shared_state = {}          # last values pushed to sensor platforms
        self._unsupported = {}           # read-name -> empty-response count

        self._attr_temperature_unit = "°C"  # correct
        self._attr_hvac_modes = modes
        self._attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF)
        self._attr_temperature_step = 1.0

        # Initial state
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_mode = FAN_LOW
        self._attr_current_temperature = None
        self._attr_target_temperature = None

        if self.hass is None:
            standalone = True
        else:
            standalone = False


    def set_online(self, online: bool):
        if online:
            if self._offline_count >= MAX_OFFLINE_COUNT:
                log_info(logger, f'reconnected', self)
            self._offline_count = 0
        else:
            self._offline_count += 1
            log_warning(logger, f'offline_count:{self._offline_count}', self)

        self.async_write_ha_state()
        self._push_shared({"online": online})


    def _push_shared(self, values: dict):
        """Merge values into the shared per-device state and notify the
        sensor / binary_sensor entities of this device."""
        self._shared_state.update(values)
        if self._hass is not None:
            async_dispatcher_send(self._hass, signal_update(self._mac), dict(self._shared_state))


    async def _read_slow(self, name, req):
        """One slow read (energy / hours / info); stops asking after
        UNSUPPORTED_AFTER consecutive empty responses, since not every
        unit supports every function."""
        if self._unsupported.get(name, 0) >= UNSUPPORTED_AFTER:
            return
        rsp = await self._madoka.device_command({"read": req})
        rsp = {k: v for k, v in rsp.items() if k in req and not k.endswith("unit_number")}
        if not rsp:
            self._unsupported[name] = self._unsupported.get(name, 0) + 1
            if self._unsupported[name] >= UNSUPPORTED_AFTER:
                log_info(logger, f'{name}: no response {UNSUPPORTED_AFTER}x, assuming unsupported - not polling it anymore', self)
            return
        self._unsupported[name] = 0
        log_debug(logger, f'{name}: {rsp}', self)
        self._push_shared(rsp)


    def set_filter_status(self, needs_clean: bool):
        self._filter_needs_clean = needs_clean
        # Update HA state so UI sees it
        self._attr_extra_state_attributes['filter_needs_clean'] = needs_clean
        self.async_write_ha_state()


    @property
    def available(self):
        ret =  False if (self._offline_count >= MAX_OFFLINE_COUNT) else True
        if not ret:
            log_warning(logger, f'is offline ({self._offline_count})', self)

        return ret


    @property
    def filter_needs_clean(self):
        return self._filter_needs_clean


    @property
    def current_temperature(self):
        return self._attr_current_temperature

    async def async_will_remove_from_hass(self):
        await self._madoka.close()

    async def madoka_connect_cb(self, connected:bool):
        log_info(logger, f'madoka_connect_cb {self._mac} {connected}', self)
        if connected:
            self.set_online(True)

    async def _write_pending(self, key, value):
        """Write a single key/value, keeping it pending for retry on the next
        async_update() if the device doesn't confirm it."""
        rsp = await self._madoka.device_command({"write": {key: value}})
        if key in rsp:
            self._pending_write.pop(key, None)
            log_info(logger, f'{key} confirmed: {rsp.get(key)}', self)
        else:
            self._pending_write[key] = value
            log_warning(logger, f'{key}={value} not confirmed (rsp:{rsp}), will retry on next update', self)

    async def async_set_temperature(self, **kwargs):
        temp = kwargs.get("temperature")
        if temp is None:
            log_error(logger, f'async_set_temperature temp is none', self)
            return

        log_info(logger, f'async_set_temperature {temp}', self)
        self._attr_target_temperature = temp

        fields = MODE_FIELDS.get(self._mode)
        if fields is None:
            log_warning(logger, f'async_set_temperature: unknown mode {self._mode!r}, ignoring', self)
            return

        await self._write_pending(fields["temp"], temp)
        self.async_write_ha_state()


    async def async_set_hvac_mode(self, hvac_mode):
        log_info(logger, f'async_set_hvac_mode hvac_mode:{hvac_mode}', self)
        self._attr_hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            req = {"write":{"power":0} }
        elif self._power_only:
            # device has no HA-selectable cool/heat mode; just power it on and
            # leave whatever mode is already set on the device untouched
            req = {"write":{"power":1} }
        else:
            mode = HVAC_MODE_TO_MODE.get(hvac_mode, "cool")
            req = {"write":{"power":1, "mode":mode} }

        log_info(logger, f'async_set_hvac_mode req:{req}', self)
        rsp = await self._madoka.device_command(req)
        log_info(logger, f'async_set_hvac_mode rsp: {rsp}', self)
        self.async_write_ha_state()


    async def async_set_fan_mode(self, fan_mode):
        log_info(logger, f'async_set_fan_mode {fan_mode}', self)

        self._attr_fan_mode = fan_mode
        log_info(logger, f'async_set_fan_mode fan_mode: {fan_mode}', self)

        fields = MODE_FIELDS.get(self._mode)
        if fields is None:
            log_warning(logger, f'async_set_fan_mode: unknown mode {self._mode!r}, ignoring', self)
            return

        await self._write_pending(fields["fan"], fan_mode)
        self.async_write_ha_state()


    async def async_update(self):
        for key, value in list(self._pending_write.items()):
            log_info(logger, f'retrying pending write {key}={value}', self)
            await self._write_pending(key, value)

        req = {"read": dict(FAST_READ)}

        rsp = await self._madoka.device_command(req)
        log_info(logger, f'async_update rsp: {rsp}', self)

        power = rsp.get("power")
        if power is not None:
            #self._attr_hvac_mode = "off" if power==0  else "cool"
            self.set_online(True)
        else:
            self.set_online(False)
            return

        mode = rsp.get("mode")
        if mode is not None:
            self._mode = mode

            if power == 0:
                self._attr_hvac_mode = HVACMode.OFF
            elif self._power_only:
                self._attr_hvac_mode = HVACMode.AUTO
            elif mode in MODE_TO_HVAC_MODE:
                self._attr_hvac_mode = MODE_TO_HVAC_MODE[mode]


        current_temp = rsp.get("temp_indoor")
        if current_temp is not None:
            self._attr_current_temperature = current_temp

        fields = MODE_FIELDS.get(mode, MODE_FIELDS["heat"])
        temp_key, fan_key = fields["temp"], fields["fan"]

        set_temp = rsp.get(temp_key)
        current_fan = rsp.get(fan_key)

        # don't let a stale read clobber a write that's still pending retry
        if current_fan is not None and fan_key not in self._pending_write:
            self._attr_fan_mode = current_fan

        if set_temp is not None and temp_key not in self._pending_write:
            self._attr_target_temperature = set_temp

        clean_filter = rsp.get("clean_filter")
        if clean_filter is not None:
            # bit 0 = clean filter; the other bits (clean element, replace
            # dustbox/filter, ...) are exposed as binary_sensors
            self.set_filter_status(bool(clean_filter & 1))

        # hand everything the sensor platforms care about over to them
        self._push_shared({k: v for k, v in rsp.items() if k in FAST_READ})

        self._poll_count += 1
        if self._poll_count % SLOW_POLL_EVERY == 1:
            for name, slow_req in SLOW_READS.items():
                await self._read_slow(name, slow_req)
        if self._poll_count % INFO_POLL_EVERY == 1:
            await self._read_slow("info", INFO_READ)
