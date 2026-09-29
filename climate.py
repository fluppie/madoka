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
from .const import DOMAIN, ALLOWED_MODES, DEFAULT_MODES, DEFAULT_PAIRING_TIMEOUT
from .madoka import Madoka
from .helpers import format_hex, log_context, log_debug, log_trace, log_info, log_warning, log_error, log_exception

logger = logging.getLogger(__name__)
logger.propagate = True

CONF_NAME    = "name"
CONF_MAC     = "mac"
CONF_MODES   = "modes"
CONF_PAIRING_TIMEOUT = "pairing_timeout"

MAX_OFFLINE_COUNT = 3

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

        req = {"read":{"power":0, "mode":0, "temp_indoor":0, "cooling":0, "heating":0, "cool_fan":0, "heat_fan":0, "clean_filter":0} }

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
            self.set_filter_status(False if (clean_filter==0) else True)
