import unittest

from ._pkgstub import load

load("helpers")
load("decoder")
protocol = load("protocol")

encode = protocol.encode
FrameParser = protocol.FrameParser


class TestEncode(unittest.TestCase):
    def test_single_field_produces_one_frame(self):
        frames = encode({"power": 1})
        self.assertEqual(len(frames), 1)
        cmd, chunks = frames[0]
        self.assertEqual(cmd, 0x20)
        # single short write fits in one chunk
        self.assertEqual(len(chunks), 1)

    def test_key_offset_is_applied_to_cmd(self):
        frames = encode({"power": 0}, key_offset=0x4000)
        cmd, chunks = frames[0]
        self.assertEqual(cmd, 0x4020)

    def test_multiple_fields_in_different_groups_produce_multiple_frames(self):
        # "power" (cmd 0x20) and "mode" (cmd 0x30) are different field-groups,
        # so each must be its own independently-sent/acked frame
        frames = encode({"power": 1, "mode": "cool"})
        cmds = {cmd for cmd, _chunks in frames}
        self.assertEqual(cmds, {0x20, 0x30})

    def test_chunks_are_indexed_starting_at_zero(self):
        frames = encode({"power": 1})
        _cmd, chunks = frames[0]
        self.assertEqual(chunks[0][0], 0)


class TestFrameParserRoundTrip(unittest.TestCase):
    def test_single_chunk_round_trip(self):
        frames = encode({"mode": "cool"})
        cmd, chunks = frames[0]

        parser = FrameParser()
        result = None
        for chunk in chunks:
            result = parser.feed(bytearray(chunk))

        self.assertIsNotNone(result)
        rx_cmd, msg = result
        self.assertEqual(rx_cmd, cmd)
        self.assertEqual(msg, {"mode": "cool"})

    def test_returns_none_until_all_chunks_fed(self):
        # cooling's 2-byte value plus header still fits in a single 19-byte
        # chunk for this protocol, so use a multi-value write to force
        # multiple chunks and confirm intermediate feeds return None. The
        # six "c00".."c05" fields are all in the same cmd group (0x00) and
        # together push the payload past the 19-byte single-chunk limit.
        many_fields = {"c00": 1, "c01": 2, "c02": 3, "c03": 4, "c04": 5, "c05": 6}
        frames = encode(many_fields)
        self.assertEqual(len(frames), 1)
        cmd, chunks = frames[0]
        self.assertGreater(len(chunks), 1, "expected this payload to span multiple chunks")

        parser = FrameParser()
        for chunk in chunks[:-1]:
            self.assertIsNone(parser.feed(bytearray(chunk)))

        result = parser.feed(bytearray(chunks[-1]))
        self.assertIsNotNone(result)
        rx_cmd, msg = result
        self.assertEqual(rx_cmd, cmd)
        self.assertEqual(msg, many_fields)

    def test_reset_discards_in_progress_reassembly(self):
        many_fields = {"c00": 1, "c01": 2, "c02": 3, "c03": 4, "c04": 5, "c05": 6}
        frames = encode(many_fields)
        _cmd, chunks = frames[0]
        self.assertGreater(len(chunks), 1)

        parser = FrameParser()
        parser.feed(bytearray(chunks[0]))  # start a packet, don't finish it
        parser.reset()

        # feeding the *second* chunk of the abandoned packet (idx != 0)
        # after a reset must not be treated as a continuation
        result = parser.feed(bytearray(chunks[1]))
        self.assertIsNone(result)

    def test_full_pipeline_round_trip_for_every_field(self):
        original = {
            "power": 1,
            "mode": "heat",
            "cooling": 24,
            "heating": 22,
            "cool_fan": "high",
            "heat_fan": "low",
            "clean_filter": 1,
            "temp_indoor": 23,
        }
        frames = encode(original)
        parser = FrameParser()
        decoded = {}
        for cmd, chunks in frames:
            result = None
            for chunk in chunks:
                result = parser.feed(bytearray(chunk))
            self.assertIsNotNone(result)
            rx_cmd, msg = result
            self.assertEqual(rx_cmd, cmd)
            decoded.update(msg)

        self.assertEqual(decoded, original)


if __name__ == "__main__":
    unittest.main()
