
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
    0x100: {
        0x62: ("clean_filter", 1, ident, to_int),
    }, 
    0x110: {
        0x40: ("temp_indoor", 1, ident, ident),
        0x41: ("temp_outdoor",1, ident, ident),
    }
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
                # validate val has expected size before int.from_bytes
                try:
                    v = int.from_bytes(val, byteorder="big")
                except Exception:
                    logger.exception(f"{context}failed to parse value for cmd:{cmd:x} type:{t:x}")
                    continue
                state[var_name] = f1(v)
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
                        #val_int = int(val)
                        data[cmd+keyOffset].append((k, bytearray(translated.to_bytes(s, "big")) ))
                        break;

        data = dict(data)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"{context}data: {format_hex(data)}")
        return data

