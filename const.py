DOMAIN = "madoka"

# "auto" stands in for devices that only support power on/off (no cool/heat
# selection from HA) - see climate.py's _power_only handling.
ALLOWED_MODES = ["off", "auto", "cool", "heat", "dry"]
DEFAULT_MODES = ["off", "cool", "heat"]

# seconds to wait for the user to confirm the pairing code on the unit's
# display before giving up on client.pair() - see madoka.py's connect().
# Overridable per-device via the "pairing_timeout" config key.
DEFAULT_PAIRING_TIMEOUT = 60


PLATFORMS = ["climate", "sensor", "binary_sensor"]


def signal_update(mac: str) -> str:
    """Dispatcher signal the climate entity (the one poller per device)
    uses to push shared state to that device's sensor entities."""
    return f"{DOMAIN}_update_{mac.lower().replace(':', '')}"


def device_info(mac: str, name: str):
    """Common device registry entry so climate, sensor and binary_sensor
    entities of one controller are grouped under a single device."""
    from homeassistant.helpers.entity import DeviceInfo
    return DeviceInfo(
        identifiers={(DOMAIN, mac.lower())},
        name=name,
        manufacturer="Daikin",
        model="BRC1H (Madoka)",
    )
