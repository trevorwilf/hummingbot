"""MQTT serializer strict-JSON tests (V2 strategy fixes phase 2 — finding A5b).

ujson emits bare ``NaN``/``Infinity`` tokens for non-finite floats — invalid JSON that
strict parsers reject — and str()-coerces Enum dict keys to ``"CloseType.X"``. The
serializer must emit None for non-finite numbers and stable ``.name`` strings for Enum
keys, while staying byte-compatible with the legacy wire format for everything else.

Every assertion parses with ``json.loads(..., parse_constant=reject)`` so a bare
NaN/Infinity token fails the test (python's json accepts them by default).
"""
import json
import unittest
from decimal import Decimal

from hummingbot.remote_iface.mqtt import _make_primitive, mqtt_serialize
from hummingbot.strategy_v2.models.executors import CloseType


def _strict_loads(serialized: str):
    def reject(constant):
        raise AssertionError(f"non-strict JSON constant emitted on the wire: {constant}")

    return json.loads(serialized, parse_constant=reject)


class TestMqttSerializerStrictJson(unittest.TestCase):
    def test_non_finite_numbers_serialize_to_null(self):
        payload = {
            "nan_decimal": Decimal("NaN"),
            "inf_decimal": Decimal("Infinity"),
            "nan_float": float("nan"),
            "inf_float": float("inf"),
            "neg_inf_float": float("-inf"),
            "finite": Decimal("1.5"),
        }
        parsed = _strict_loads(mqtt_serialize(payload))
        self.assertIsNone(parsed["nan_decimal"])
        self.assertIsNone(parsed["inf_decimal"])
        self.assertIsNone(parsed["nan_float"])
        self.assertIsNone(parsed["inf_float"])
        self.assertIsNone(parsed["neg_inf_float"])
        self.assertEqual(1.5, parsed["finite"])

    def test_non_finite_numbers_nested_in_lists_and_dicts(self):
        payload = {"positions": [{"unrealized_pnl_quote": Decimal("NaN")}, {"unrealized_pnl_quote": Decimal("3")}]}
        parsed = _strict_loads(mqtt_serialize(payload))
        self.assertIsNone(parsed["positions"][0]["unrealized_pnl_quote"])
        self.assertEqual(3.0, parsed["positions"][1]["unrealized_pnl_quote"])

    def test_enum_dict_keys_use_stable_name(self):
        payload = {"close_type_counts": {CloseType.TAKE_PROFIT: 2, CloseType.STOP_LOSS: 1}}
        parsed = _strict_loads(mqtt_serialize(payload))
        self.assertEqual({"TAKE_PROFIT": 2, "STOP_LOSS": 1}, parsed["close_type_counts"])

    def test_enum_values_keep_legacy_str_representation(self):
        # Enum VALUES stay str()-formatted for wire compatibility; only KEYS are normalized.
        parsed = _strict_loads(mqtt_serialize({"close_type": CloseType.FAILED}))
        self.assertEqual("CloseType.FAILED", parsed["close_type"])

    def test_legacy_wire_format_preserved(self):
        payload = {
            "an_int": 5,
            "a_bool": True,
            "a_none": None,
            "a_str": "x",
            "a_list": [Decimal("2"), (1, 2)],
            "a_negative_int": -3,  # legacy quirk: non-digit ints are stringified
        }
        parsed = _strict_loads(mqtt_serialize(payload))
        self.assertEqual(5, parsed["an_int"])
        self.assertIs(True, parsed["a_bool"])
        self.assertIsNone(parsed["a_none"])
        self.assertEqual("x", parsed["a_str"])
        self.assertEqual([2.0, [1, 2]], parsed["a_list"])
        self.assertEqual("-3", parsed["a_negative_int"])

    def test_performance_report_like_payload_round_trips(self):
        payload = {
            "main": {
                "performance": {
                    "realized_pnl_quote": Decimal("1.2"),
                    "unrealized_pnl_quote": Decimal("NaN"),
                    "close_type_counts": {CloseType.TAKE_PROFIT: 4},
                    "positions_summary": [{"breakeven_price": Decimal("Infinity")}],
                },
                "custom_info": {"side": CloseType.POSITION_HOLD},
            }
        }
        parsed = _strict_loads(mqtt_serialize(payload))
        performance = parsed["main"]["performance"]
        self.assertEqual(1.2, performance["realized_pnl_quote"])
        self.assertIsNone(performance["unrealized_pnl_quote"])
        self.assertEqual({"TAKE_PROFIT": 4}, performance["close_type_counts"])
        self.assertIsNone(performance["positions_summary"][0]["breakeven_price"])

    def test_make_primitive_leaves_plain_string_keys_untouched(self):
        self.assertEqual({"a": 1.0}, _make_primitive({"a": Decimal("1")}))


if __name__ == "__main__":
    unittest.main()
