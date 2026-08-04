"""High-level codec for the Madoka BLE GATT protocol.

This is the single place that knows how to turn a plain dict of field
names/values (the vocabulary climate.py and madoka.py work in, e.g.
{"mode": "cool"} or {"cooling": 24}) into the raw binary frame(s) written to
the GATT characteristic, and back again. Callers never need to touch command
codes, TLV types, or chunk indices directly - that's all encapsulated here
(on top of decoder.py's field <-> TLV mapping table).

No BLE or Home Assistant dependency, so both directions are directly
unit-testable without any real connection.
"""
import logging

from .decoder import Decoder
from .helpers import format_hex, log_warning

logger = logging.getLogger(__name__)
logger.propagate = True

CHUNK_SIZE = 19


def encode(msg: dict, key_offset: int = 0, context: str = ""):
    """{"mode": "cool"} -> [(cmd, [chunk0, chunk1, ...]), ...]

    One (cmd, chunks) pair per distinct field-group present in msg, since
    each group is written and acknowledged as its own independent frame on
    the wire (see madoka.py's process(), which sends each pair separately).
    """
    tx = Decoder.parsedToData(msg, key_offset, context)
    return [(cmd, _build_chunks(cmd, parts)) for cmd, parts in tx.items()]


def _build_chunks(cmd: int, parts):
    data = bytearray([0x00]) + cmd.to_bytes(2, "big")
    for t, v in parts:
        data.append(t)
        data.append(len(v))
        data.extend(v)
    data.insert(0, len(data) + 1)

    chunks = []
    idx = 0
    while True:
        chunk = data[:CHUNK_SIZE]
        if len(chunk) == 0:
            break
        chunk.insert(0, idx)
        del data[:CHUNK_SIZE]
        chunks.append(chunk)
        idx += 1
    return chunks


class FrameParser:
    """Synchronous, stateful reassembly of one BLE notification stream into
    decoded messages.

    Feed it raw chunks as they arrive (feed()); once a full frame has been
    reassembled, returns (cmd, msg_dict) - e.g. (0x30, {"mode": "cool"}) -
    or None while more chunks are still expected. No threading/asyncio
    dependency; callers needing that (see framer.py) wrap this.
    """

    def __init__(self, context: str = ""):
        self._context = context
        self.rxData = bytearray()
        self.expectedSize = 0
        self.expectedIdx = 0

    def reset(self):
        self.rxData = bytearray()
        self.expectedSize = 0
        self.expectedIdx = 0

    def feed(self, data: bytearray):
        if not data or len(data) < 1:
            log_warning(logger, f"{self._context}FrameParser.feed: empty/invalid packet")
            return None

        idx = data[0]
        chunk = data[1:]

        if idx == 0:
            if len(chunk) < 1:
                log_warning(logger, f"{self._context}FrameParser.feed: invalid header chunk")
                return None
            self.expectedSize = chunk[0]
            self.rxData = bytearray()
        else:
            if self.expectedIdx != idx:
                log_warning(logger, f"{self._context}FrameParser.feed: unexpected idx:{idx}, expecting {self.expectedIdx}")
                return None

        self.expectedIdx = idx + 1
        self.rxData.extend(chunk)

        if len(self.rxData) < self.expectedSize:
            return None

        payload = self.rxData[2:]
        if len(payload) < 2:
            log_warning(logger, f"{self._context}FrameParser.feed: payload too short for cmd")
            return None

        cmd = int.from_bytes(payload[0:2], byteorder="big")
        tlvData = self.rxData[4:]

        values = []
        while len(tlvData) > 2:
            t = tlvData[0]
            l = tlvData[1]
            if len(tlvData) < 2 + l:
                log_warning(logger, f"{self._context}FrameParser.feed: truncated value for type t:{t} l:{l} len:{len(tlvData)}")
                break
            v = tlvData[2:2+l]
            del tlvData[:2+l]
            values.append((t, v))

        return cmd, Decoder.parse(cmd, values, self._context)
