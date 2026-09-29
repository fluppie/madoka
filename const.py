DOMAIN = "madoka"

# "auto" stands in for devices that only support power on/off (no cool/heat
# selection from HA) - see climate.py's _power_only handling.
ALLOWED_MODES = ["off", "auto", "cool", "heat", "dry"]
DEFAULT_MODES = ["off", "cool", "heat"]

# seconds to wait for the user to confirm the pairing code on the unit's
# display before giving up on client.pair() - see madoka.py's connect().
# Overridable per-device via the "pairing_timeout" config key.
DEFAULT_PAIRING_TIMEOUT = 60

