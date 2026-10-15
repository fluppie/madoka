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


class TestExtendedDecoders(unittest.TestCase):
    """Decoders for the sensor / binary_sensor platforms."""

    def test_temp_positive(self):
        self.assertEqual(Decoder.parse(0x110, [(0x41, bytes([7]))]), {"temp_outdoor": 7})

    def test_temp_negative_is_128_minus_value(self):
        # value > 126 -> 128 - value; 0x83 = 131 -> -3
        self.assertEqual(Decoder.parse(0x110, [(0x41, bytes([0x83]))]), {"temp_outdoor": -3})

    def test_temp_no_sensor_is_none(self):
        self.assertEqual(Decoder.parse(0x110, [(0x41, bytes([0xFF]))]), {"temp_outdoor": None})
        self.assertEqual(Decoder.parse(0x110, [(0x40, bytes([0x7F]))]), {"temp_indoor": None})

    def test_energy_today_uint32le_tenths_kwh(self):
        # total 123 (12.3 kWh) + two 2h slots of 5 and 6 (0.5 / 0.6 kWh)
        raw = (123).to_bytes(4, "little") + (5).to_bytes(4, "little") + (6).to_bytes(4, "little")
        state = Decoder.parse(0x120, [(0x40, raw)])
        self.assertEqual(state, {"energy_today": {"total": 12.3, "slots": [0.5, 0.6]}})

    def test_energy_empty_is_none(self):
        self.assertEqual(Decoder.parse(0x120, [(0x41, b"")]), {"energy_yesterday": None})

    def test_operation_hours(self):
        raw = (4321).to_bytes(4, "little")
        self.assertEqual(Decoder.parse(0x112, [(0x40, raw)]), {"hours_indoor": 4321})

    def test_operation_hours_unavailable(self):
        self.assertEqual(Decoder.parse(0x112, [(0x46, bytes([0xFF] * 4))]), {"hours_compressor1": None})

    def test_error_code(self):
        # [unit][ascii][ascii][number] -> "U4-01" on unit 0
        state = Decoder.parse(0x100, [(0x90, bytes([0, ord("U"), ord("4"), 1]))])
        self.assertEqual(state, {"error_code": {"unit": 0, "code": "U4-01"}})

    def test_error_code_none_when_empty(self):
        self.assertEqual(Decoder.parse(0x100, [(0x90, bytes([0, 0, 0, 0]))]), {"error_code": None})

    def test_notification_bits_kept_raw(self):
        self.assertEqual(Decoder.parse(0x100, [(0x50, bytes([0b11]))]), {"notif_errors": 3})
        self.assertEqual(decoder.NOTIFICATION_BITS["notif_errors"][0], "error")

    def test_model_info_ascii(self):
        self.assertEqual(Decoder.parse(0x130, [(0x41, b"FXFQ50A\x00\x00")]), {"model_info": "FXFQ50A"})

    def test_read_request_for_raw_field_carries_one_zero_byte(self):
        data = Decoder.parsedToData({"energy_today": 0})
        self.assertEqual(data, {0x120: [(0x40, bytearray([0]))]})

    def test_hours_request_includes_unit_number_in_same_frame(self):
        data = Decoder.parsedToData({"unit_number": 0, "hours_indoor": 0})
        self.assertEqual(data, {0x112: [(0x02, bytearray([0])), (0x40, bytearray([0]))]})
