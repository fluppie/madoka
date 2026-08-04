import unittest

from ._pkgstub import load

load("helpers")
decoder = load("decoder")

Decoder = decoder.Decoder
mode_map = decoder.mode_map
fan_map = decoder.fan_map
div128 = decoder.div128
mul128 = decoder.mul128


class TestScaling(unittest.TestCase):
    def test_div128_exact(self):
        self.assertEqual(div128(25 * 128), 25)

    def test_mul128(self):
        self.assertEqual(mul128(25), 25 * 128)

    def test_div128_truncates_towards_zero_for_non_multiples(self):
        # 3201 / 128 = 25.0078..., int() truncates rather than rounds
        self.assertEqual(div128(3201), 25)


class TestModeAndFanMaps(unittest.TestCase):
    def test_mode_map_values(self):
        self.assertEqual(mode_map[3], "cool")
        self.assertEqual(mode_map[4], "heat")

    def test_fan_map_values(self):
        self.assertEqual(fan_map[1], "low")
        self.assertEqual(fan_map[5], "high")


class TestDecoderParse(unittest.TestCase):
    def test_parse_power(self):
        state = Decoder.parse(0x20, [(0x20, bytes([1]))])
        self.assertEqual(state, {"power": 1})

    def test_parse_mode(self):
        state = Decoder.parse(0x30, [(0x20, bytes([3]))])
        self.assertEqual(state, {"mode": "cool"})

    def test_parse_cooling_scales_by_128(self):
        # 25 degrees -> 25*128 = 3200 = 0x0C80
        state = Decoder.parse(0x40, [(0x20, bytes([0x0C, 0x80]))])
        self.assertEqual(state, {"cooling": 25})

    def test_parse_cool_fan(self):
        state = Decoder.parse(0x50, [(0x20, bytes([5]))])
        self.assertEqual(state, {"cool_fan": "high"})

    def test_parse_clean_filter(self):
        state = Decoder.parse(0x100, [(0x62, bytes([1]))])
        self.assertEqual(state, {"clean_filter": 1})

    def test_parse_temp_indoor(self):
        state = Decoder.parse(0x110, [(0x40, bytes([22]))])
        self.assertEqual(state, {"temp_indoor": 22})

    def test_parse_masks_write_offset_bit(self):
        # writes are sent/acked with a 0x4000 offset added to cmd; parse()
        # must strip that before looking the cmd up in parseTable
        state = Decoder.parse(0x4020, [(0x20, bytes([1]))])
        self.assertEqual(state, {"power": 1})

    def test_parse_unknown_cmd_returns_empty(self):
        self.assertEqual(Decoder.parse(0x9999, [(0x20, bytes([1]))]), {})

    def test_parse_unknown_type_within_known_cmd_is_skipped(self):
        # 0x99 isn't a registered type under the 0x20 (power) cmd
        state = Decoder.parse(0x20, [(0x99, bytes([1]))])
        self.assertEqual(state, {})

    def test_parse_multiple_values_in_one_packet(self):
        state = Decoder.parse(0x00, [(0x00, bytes([1])), (0x01, bytes([2]))])
        self.assertEqual(state, {"c00": 1, "c01": 2})


class TestParsedToData(unittest.TestCase):
    def test_power_write(self):
        data = Decoder.parsedToData({"power": 1})
        self.assertEqual(data, {0x20: [(0x20, bytearray([1]))]})

    def test_key_offset_is_added_to_cmd(self):
        data = Decoder.parsedToData({"power": 0}, keyOffset=0x4000)
        self.assertEqual(data, {0x4020: [(0x20, bytearray([0]))]})

    def test_cooling_encodes_scaled_value(self):
        data = Decoder.parsedToData({"cooling": 25})
        self.assertEqual(data, {0x40: [(0x20, bytearray([0x0C, 0x80]))]})

    def test_mode_encodes_via_reverse_map(self):
        data = Decoder.parsedToData({"mode": "heat"})
        self.assertEqual(data, {0x30: [(0x20, bytearray([4]))]})

    def test_unknown_field_name_is_silently_dropped(self):
        data = Decoder.parsedToData({"not_a_real_field": 5})
        self.assertEqual(data, {})

    def test_unknown_mode_string_encodes_to_default(self):
        # MapEncode falls back to its default (0) when there's no
        # reverse-mapping entry - documents current behavior (0 happens to
        # decode back as "fan", which is probably not what you'd want if
        # this were ever hit with real user input).
        data = Decoder.parsedToData({"mode": "not_a_real_mode"})
        self.assertEqual(data, {0x30: [(0x20, bytearray([0]))]})


class TestRoundTrip(unittest.TestCase):
    def test_parse_and_parsedToData_round_trip(self):
        original = {
            "power": 1,
            "mode": "cool",
            "cooling": 24,
            "cool_fan": "medium",
            "clean_filter": 0,
            "temp_indoor": 23,
        }
        encoded = Decoder.parsedToData(original)
        decoded = {}
        for cmd, values in encoded.items():
            decoded.update(Decoder.parse(cmd, values))
        self.assertEqual(decoded, original)

    def test_round_trip_with_write_key_offset(self):
        original = {"power": 1, "mode": "heat"}
        encoded = Decoder.parsedToData(original, keyOffset=0x4000)
        decoded = {}
        for cmd, values in encoded.items():
            decoded.update(Decoder.parse(cmd, values))
        self.assertEqual(decoded, original)


if __name__ == "__main__":
    unittest.main()
