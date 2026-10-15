"""Sensor entities for the Madoka BRC1H: outdoor temperature, energy
consumption, operation hours, error code and model/firmware info.

These entities don't poll the controller themselves - the climate entity is
the single poller per device and pushes everything here via the dispatcher
(see climate.py's _push_shared), so the BLE connection is shared.
Uses protocol functions 0x100, 0x110, 0x112, 0x120 and 0x130.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfTemperature, UnitOfEnergy, UnitOfTime, EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import signal_update, device_info

logger = logging.getLogger(__name__)
logger.propagate = True


@dataclass(frozen=True, kw_only=True)
class MadokaSensorDescription(SensorEntityDescription):
    # shared-state key this sensor reads
    source: str
    # optional: sub-key inside a dict value (e.g. energy -> "total")
    sub: str | None = None
    # extra attributes: shared-state key -> attribute name
    attrs: dict | None = None


SENSORS: tuple[MadokaSensorDescription, ...] = (
    MadokaSensorDescription(
        key="temp_outdoor", source="temp_outdoor", translation_key="temp_outdoor",
        name="Outdoor temperature",
        device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    MadokaSensorDescription(
        key="energy_today", source="energy_today", sub="total", name="Energy today",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    MadokaSensorDescription(
        key="energy_yesterday", source="energy_yesterday", sub="total", name="Energy yesterday",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    MadokaSensorDescription(
        key="energy_this_week", source="energy_this_week", sub="total", name="Energy this week",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    MadokaSensorDescription(
        key="energy_this_year", source="energy_this_year", sub="total", name="Energy this year",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    MadokaSensorDescription(
        key="hours_indoor", source="hours_indoor", name="Indoor unit operation hours",
        device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MadokaSensorDescription(
        key="hours_indoor_fan", source="hours_indoor_fan", name="Indoor fan hours",
        device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MadokaSensorDescription(
        key="hours_outdoor", source="hours_outdoor", name="Outdoor unit operation hours",
        device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MadokaSensorDescription(
        key="hours_compressor1", source="hours_compressor1", name="Compressor hours",
        device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MadokaSensorDescription(
        key="error_code", source="error_code", sub="code", name="Error code",
        icon="mdi:alert-circle-outline", entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MadokaSensorDescription(
        key="model_info", source="model_info", name="Model",
        icon="mdi:air-conditioner", entity_category=EntityCategory.DIAGNOSTIC,
        attrs={"fw_version": "firmware_version", "ble_version": "ble_version"},
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    config = entry.options or entry.data
    entities = []
    for dev in config.get("devices", []):
        name, mac = dev.get("name"), dev.get("mac")
        if not mac:
            continue
        entities.extend(MadokaSensor(name, mac, d) for d in SENSORS)
    async_add_entities(entities)


class MadokaSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, dev_name: str, mac: str, description: MadokaSensorDescription):
        self.entity_description = description
        self._mac = mac
        self._attr_unique_id = f"madoka_{mac}_{description.key}"
        self._attr_device_info = device_info(mac, dev_name)
        self._attr_native_value = None
        self._attr_available = False
        self._seen = False       # hide "unknown" until the device ever reported it
        self._unsub = None

    async def async_added_to_hass(self):
        self._unsub = async_dispatcher_connect(self.hass, signal_update(self._mac), self._handle_update)

    async def async_will_remove_from_hass(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _handle_update(self, state: dict):
        d = self.entity_description
        online = state.get("online", True)

        if d.source in state:
            value = state[d.source]
            if d.sub is not None and isinstance(value, dict):
                extra = {k: v for k, v in value.items() if k != d.sub}
                value = value.get(d.sub)
                if extra:
                    self._attr_extra_state_attributes = extra
            self._attr_native_value = value
            self._seen = True

        if d.attrs:
            attrs = dict(getattr(self, "_attr_extra_state_attributes", None) or {})
            for src, attr in d.attrs.items():
                if src in state:
                    attrs[attr] = state[src]
            self._attr_extra_state_attributes = attrs

        # a sensor the unit never answered for stays unavailable, so users
        # see at a glance which functions this controller supports
        self._attr_available = online and self._seen
        self.async_write_ha_state()
