DOMAIN = "madoka"

# "auto" stands in for devices that only support power on/off (no cool/heat
# selection from HA) - see climate.py's _power_only handling.
ALLOWED_MODES = ["off", "auto", "cool", "heat", "dry"]
DEFAULT_MODES = ["off", "cool", "heat"]

