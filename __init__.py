from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import logging
from . import pairing_agent
from .const import DOMAIN, PLATFORMS


_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry):
    _LOGGER.info(f'__init__ async_setup_entry {entry} ')
    hass.data.setdefault(DOMAIN, {})

    # register a Just Works BLE pairing agent once for the whole integration
    # (not per-device), so new devices pair automatically on first connect
    # instead of requiring a manual `bluetoothctl pair` beforehand. Failure
    # here is non-fatal - it just means pairing may need to be done manually.
    if "_pairing_bus" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["_pairing_bus"] = await pairing_agent.register()

    # options (set via the options flow) take precedence over the original
    # data the entry was created with, so later edits actually apply
    config = entry.options or entry.data

    # Store entry-specific data (config, API clients, etc.)
    hass.data[DOMAIN][entry.entry_id] = {
        "config": config,
        "devices": config.get("devices", []),
        # you can also store API clients, Bluetooth handles, etc.
    }

    # reload the entry (tearing down and recreating devices) whenever its
    # options are updated, so edits made after install take effect
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    # Forward setup to the climate platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Handle removal."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

        # only one config entry is supported, so unloading it means no more
        # devices need the pairing agent
        bus = hass.data[DOMAIN].pop("_pairing_bus", None)
        await pairing_agent.unregister(bus)

    return unload_ok

