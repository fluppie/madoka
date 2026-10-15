
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)
logger.propagate = True

from .helpers import format_hex, log_context, TRACE_LEVEL

#mode
mode_map = {
    0: "fan",
    1: "dry",
    2: "auto",
    3: "cool",
    4: "heat",
    5: "vent"
}

#fan mode
fan_map = {
    0: "auto",
    1: "low",
    3: "medium",
    5: "high",
}

rev_mode_map = defaultdict(list)
rev_fan_map = defaultdict(list)

rev_mode_map = {v: k for k, v in mode_map.items()}
rev_fan_map = {v: k for k, v in fan_map.items()}

# logger.info(f'rev_mode: {rev_mode_map}')
# logger.info(f'rev_fan : {rev_fan_map}')

# identity
def ident(v):
    return v

# maps
def mode_decode(v, mode_map):
    return mode_map.get(v, "unknown")

def mode_encode(v, rev_mode_map):
    return rev_mode_map.get(v, 0)

def fan_decode(v, fan_map):
    return fan_map.get(v, "unknown")

def fan_encode(v, rev_fan_map):
    return rev_fan_map.get(v, 0)

# scaling
def div128(v):
    return int(v / 128)

def mul128(v):
    return int(v * 128)

def to_int(v):
    return int(v)

# Temperature byte encoding of the controller:
# 0xFF / 0x7F mean "no sensor / not available", anything above 126 is a
# negative reading encoded as 128 - v (0x83 -> -3), otherwise the plain value.
def temp_decode(v):
    if v in (0xFF, 0x7F):
        return None
    if v > 126:
        return 128 - v
    return v


# ---- raw-bytes decoders (used with size 0 in parseTable, see Decoder.parse)

def _u32le_list(raw):
    """Split raw bytes into uint32 little-endian values (per 4-byte group)."""
    return [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw) - len(raw) % 4, 4)]


# Energy consumption (cmd 0x120): a list of uint32 LE in 0.1 kWh, first
# entry is the period total, the rest the per-slot breakdown
# (daily: 12 x 2h slots, weekly: 7 days sun..sat, yearly: 12 months).
def energy_decode(raw):
    vals = _u32le_list(raw)
    if not vals:
        return None
    return {"total": vals[0] / 10, "slots": [v / 10 for v in vals[1:]]}


# Operation hours (cmd 0x112): uint32 LE hours, 0xFFFFFFFF = not available.
def hours_decode(raw):
    vals = _u32le_list(raw)
    if not vals or vals[0] == 0xFFFFFFFF:
        return None
    return vals[0]


# Current error code (cmd 0x100 type 0x90/0x91): [unit][ascii][ascii][number]
# -> e.g. "U4-01"; all-zero / wrong length means no error.
def error_code_decode(raw):
    if len(raw) != 4:
        return None
    unit, c1, c2, num = raw
    if c1 == 0 and c2 == 0:
        return None
    try:
        code = f"{chr(c1)}{chr(c2)}-{num:02d}"
    except ValueError:
        return None
    return {"unit": unit, "code": code}


# Model info (cmd 0x130 type 0x41): ASCII, NUL padded; may hold indoor and
# outdoor model names. Return the printable string.
def ascii_decode(raw):
    return bytes(raw).split(b"\x00")[0].decode("ascii", errors="replace").strip() or None


def version_decode(raw):
    return ".".join(str(b) for b in raw) if raw else None


class MapDecode:
    def __init__(self, m):
        self.m = m
    def __call__(self, v):
        return self.m.get(v, "unknown")

class MapEncode:
    def __init__(self, m, default=0):
        self.m = m
        self.default = default
    def __call__(self, v):
        return self.m.get(v, self.default)


parseTable = {
    0x00: {
        0x00: ("c00", 1, ident, ident),
        0x01: ("c01", 1, ident, ident),
        0x02: ("c02", 1, ident, ident),
        0x03: ("c03", 1, ident, ident),
        0x04: ("c04", 1, ident, ident),
        0x05: ("c05", 1, ident, ident),
        0x10: ("c10", 1, ident, ident),
        0x11: ("c11", 1, ident, ident),
        0x12: ("c12", 1, ident, ident),
        0x13: ("c13", 1, ident, ident),
        0x14: ("c14", 1, ident, ident),
        0x15: ("c15", 1, ident, ident),
        0x16: ("c16", 1, ident, ident),
        0x17: ("c17", 1, ident, ident),
        0x40: ("c40", 1, ident, ident),
    },
    0x20: {
        0x20: ("power", 1, ident, ident),
    },
    0x30: {
        0x20: ("mode", 1, MapDecode(mode_map), MapEncode(rev_mode_map, default=0)),
    },
    0x40: {
        0x20: ("cooling", 2, div128, mul128),
        0x21: ("heating", 2, div128, mul128),
    },
    0x50: {
        0x20: ("cool_fan", 1, MapDecode(fan_map), MapEncode(rev_fan_map, default=0)),
        0x21: ("heat_fan", 1, MapDecode(fan_map), MapEncode(rev_fan_map, default=0)),
    },
    # notifications - bit flags, decoded into individual flags by the
    # binary_sensor platform (see NOTIFICATION_BITS below)
    0x100: {
        0x40: ("notif_general", 1, ident, to_int),
        0x41: ("notif_special", 1, ident, to_int),
        0x42: ("notif_onoff", 1, ident, to_int),
        0x50: ("notif_errors", 1, ident, to_int),
        0x61: ("notif_schedule", 1, ident, to_int),
        0x62: ("clean_filter", 1, ident, to_int),
        0x90: ("error_code", 0, error_code_decode, ident),
    },
    0x110: {
        0x40: ("temp_indoor", 1, temp_decode, ident),
        0x41: ("temp_outdoor",1, temp_decode, ident),
    },
    # operation hours; the request must carry the unit number (type 0x02)
    0x112: {
        0x02: ("unit_number", 1, ident, to_int),
        0x40: ("hours_indoor", 0, hours_decode, ident),
        0x41: ("hours_indoor_fan", 0, hours_decode, ident),
        0x42: ("hours_indoor_power", 0, hours_decode, ident),
        0x43: ("hours_outdoor", 0, hours_decode, ident),
        0x44: ("hours_outdoor_fan1", 0, hours_decode, ident),
        0x45: ("hours_outdoor_fan2", 0, hours_decode, ident),
        0x46: ("hours_compressor1", 0, hours_decode, ident),
        0x47: ("hours_compressor2", 0, hours_decode, ident),
    },
    # energy consumption - each is a large multi-chunk response, request
    # them one at a time (a combined response would exceed one frame)
    0x120: {
        0x40: ("energy_today", 0, energy_decode, ident),
        0x41: ("energy_yesterday", 0, energy_decode, ident),
        0x42: ("energy_this_week", 0, energy_decode, ident),
        0x43: ("energy_last_week", 0, energy_decode, ident),
        0x44: ("energy_this_year", 0, energy_decode, ident),
        0x45: ("energy_last_year", 0, energy_decode, ident),
    },
    0x130: {
        # field names are looked up by name across the whole table when
        # encoding, so this one must not clash with 0x112's unit_number
        0x02: ("info_unit_number", 1, ident, to_int),
        0x41: ("model_info", 0, ascii_decode, ident),
        0x45: ("fw_version", 0, version_decode, ident),
        0x46: ("ble_version", 0, version_decode, ident),
    },
}

# bit layout of the 0x100 notification bytes
NOTIFICATION_BITS = {
    "notif_general": {0: "centralized_control", 1: "forced_thermo_off", 2: "backup_operation", 3: "commissioning"},
    "notif_special": {0: "quick_start", 1: "test_operation", 2: "defrost_hot_start", 3: "duty_rotation"},
    "notif_onoff":   {0: "setback_active"},
    "notif_errors":  {0: "error", 1: "warning", 2: "alarm", 4: "refrigerant_leak"},
    "notif_schedule": {0: "schedule_enabled", 1: "holiday_enabled"},
    "clean_filter":  {0: "clean_filter", 1: "clean_element", 2: "self_cleaning_filter", 3: "replace_dustbox", 4: "replace_filter"},
}

class Decoder:
    @staticmethod
    def parse(cmd, values, context=""):
        state = {}

        cmd &= 0x3fff

        table = parseTable.get(cmd)
        if table is None:
            logger.error(f"{context}command {cmd} not supporeted")
            return {}

        if logger.isEnabledFor(TRACE_LEVEL):
            logger.trace(f"{context}parse cmd:{cmd} values:{format_hex(values)}")

        for t, val in values:
            if t in table:
                if logger.isEnabledFor(TRACE_LEVEL):
                    logger.trace(f"{context}parse type: {t}, val:{format_hex(val)}")
                var_name, s, f1, f2 = table[t]
                if logger.isEnabledFor(TRACE_LEVEL):
                    logger.trace(f"{context}name: {var_name}")
                try:
                    if s == 0:
                        # raw-bytes field: decoder gets the value bytes as-is
                        state[var_name] = f1(bytes(val))
                    else:
                        v = int.from_bytes(val, byteorder="big")
                        state[var_name] = f1(v)
                except Exception:
                    logger.exception(f"{context}failed to parse value for cmd:{cmd:x} type:{t:x}")
                    continue
                #logger.info(f"var_name: {var_name} val:{state[var_name]}")

        # state is a dict; empty state is acceptable (no matching types)
        if logger.isEnabledFor(TRACE_LEVEL):
            logger.trace(f"{context}state: {state}")
        return state

                                                                                                                                                                                                                                        
    @staticmethod
    def parsedToData(msg, keyOffset=0, context=""):
        #logger.debug(f"{context}parsedToData: msg:{msg}, keyOffset:{keyOffset:x}")
        data = defaultdict(list)
        for name, val in msg.items():
            for cmd, values in parseTable.items():
                for k, (n, s, f1, f2) in values.items():
                    #logger.info(f"name:{name} {cmd:x} {k:x} n:{n}")
                    if (name == n):
                        translated = int(f2(val))
                        if logger.isEnabledFor(TRACE_LEVEL):
                            logger.trace(f'{context}found {n} cmd:{cmd:x} k:{k:x} val:{format_hex(val)} ({translated})')
                        # raw-bytes (size 0) fields are read-only; a read
                        # request still carries a 1-byte zero value
                        data[cmd+keyOffset].append((k, bytearray(translated.to_bytes(max(s, 1), "big")) ))
                        break;

        data = dict(data)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"{context}data: {format_hex(data)}")
        return data

