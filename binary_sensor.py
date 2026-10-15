"""Binary sensors for the Madoka BRC1H, decoded from the notification bit
fields of protocol function 0x100 (bit layout in decoder.NOTIFICATION_BITS).

Like sensor.py, these are push-only: the climate entity polls and
dispatches the shared state.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import signal_update, device_info
from .decoder import NOTIFICATION_BITS

logger = logging.getLogger(__name__)
logger.propagate = True


@dataclass(frozen=True, kw_only=True)
class MadokaBinaryDescription(BinarySensorEntityDescription):
    source: str   # shared-state key holding the bit field
    bit: int      # bit to test


def _bit(source, flag):
    """Look the bit number up by flag name so the layout lives in one place."""
    for b, n in NOTIFICATION_BITS[source].items():
        if n == flag:
            return b
    raise KeyError(flag)


BINARY_SENSORS: tuple[MadokaBinaryDescription, ...] = (
    MadokaBinaryDescription(key="error", source="notif_errors", bit=_bit("notif_errors", "error"),
                            name="Error", device_class=BinarySensorDeviceClass.PROBLEM),
    MadokaBinaryDescription(key="warning", source="notif_errors", bit=_bit("notif_errors", "warning"),
                            name="Warning", device_class=BinarySensorDeviceClass.PROBLEM),
    MadokaBinaryDescription(key="alarm", source="notif_errors", bit=_bit("notif_errors", "alarm"),
                            name="Alarm", device_class=BinarySensorDeviceClass.PROBLEM),
    MadokaBinaryDescription(key="refrigerant_leak", source="notif_errors", bit=_bit("notif_errors", "refrigerant_leak"),
                            name="Refrigerant leak", device_class=BinarySensorDeviceClass.PROBLEM,
                            entity_registry_enabled_default=False),

    MadokaBinaryDescription(key="clean_filter", source="clean_filter", bit=_bit("clean_filter", "clean_filter"),
                            name="Filter needs cleaning", device_class=BinarySensorDeviceClass.PROBLEM, icon="mdi:air-filter"),
    MadokaBinaryDescription(key="clean_element", source="clean_filter", bit=_bit("clean_filter", "clean_element"),
                            name="Element needs cleaning", device_class=BinarySensorDeviceClass.PROBLEM,
                            entity_registry_enabled_default=False),
    MadokaBinaryDescription(key="self_cleaning_filter", source="clean_filter", bit=_bit("clean_filter", "self_cleaning_filter"),
                            name="Self-cleaning filter running", device_class=BinarySensorDeviceClass.RUNNING,
                            entity_registry_enabled_default=False),
    MadokaBinaryDescription(key="replace_dustbox", source="clean_filter", bit=_bit("clean_filter", "replace_dustbox"),
                            name="Dust box full", device_class=BinarySensorDeviceClass.PROBLEM,
                            entity_registry_enabled_default=False),
    MadokaBinaryDescription(key="replace_filter", source="clean_filter", bit=_bit("clean_filter", "replace_filter"),
                            name="Filter needs replacing", device_class=BinarySensorDeviceClass.PROBLEM,
                            entity_registry_enabled_default=False),

    MadokaBinaryDescription(key="setback_active", source="notif_onoff", bit=_bit("notif_onoff", "setback_active"),
                            name="Setback active", icon="mdi:thermometer-low"),
    MadokaBinaryDescription(key="defrost_hot_start", source="notif_special", bit=_bit("notif_special", "defrost_hot_start"),
                            name="Defrost / hot start", icon="mdi:snowflake-melt"),
    MadokaBinaryDescription(key="centralized_control", source="notif_general", bit=_bit("notif_general", "centralized_control"),
                            name="Centralized control", icon="mdi:office-building-cog",
                            entity_category=EntityCategory.DIAGNOSTIC),
    MadokaBinaryDescription(key="forced_thermo_off", source="notif_general", bit=_bit("notif_general", "forced_thermo_off"),
                            name="Forced thermo off", entity_category=EntityCategory.DIAGNOSTIC,
                            entity_registry_enabled_default=False),
    MadokaBinaryDescription(key="schedule_enabled", source="notif_schedule", bit=_bit("notif_schedule", "schedule_enabled"),
                            name="Schedule enabled", icon="mdi:calendar-clock"),
)


async def async_setup_entry(hass, entry, async_add_entities):
    config = entry.options or entry.data
    entities = []
    for dev in config.get("devices", []):
        name, mac = dev.get("name"), dev.get("mac")
        if not mac:
            continue
        entities.extend(MadokaBinarySensor(name, mac, d) for d in BINARY_SENSORS)
    async_add_entities(entities)


class MadokaBinarySensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, dev_name: str, mac: str, description: MadokaBinaryDescription):
        self.entity_description = description
        self._mac = mac
        self._attr_unique_id = f"madoka_{mac}_{description.key}"
        self._attr_device_info = device_info(mac, dev_name)
        self._attr_is_on = None
        self._attr_available = False
        self._seen = False
        self._unsub = None

    async def async_added_to_hass(self):
        self._unsub = async_dispatcher_connect(self.hass, signal_update(self._mac), self._handle_update)

    async def async_will_remove_from_hass(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _handle_update(self, state: dict):
        d = self.entity_description
        raw = state.get(d.source)
        if isinstance(raw, int):
            self._attr_is_on = bool(raw & (1 << d.bit))
            self._seen = True
        self._attr_available = state.get("online", True) and self._seen
        self.async_write_ha_state()
