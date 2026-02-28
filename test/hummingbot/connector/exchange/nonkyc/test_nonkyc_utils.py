from unittest import TestCase

from hummingbot.connector.exchange.nonkyc.nonkyc_utils import convert_fromiso_to_unix_timestamp, is_market_active


class NonkycUtilsTests(TestCase):

    def test_convert_fromiso_to_unix_timestamp_with_z_suffix(self):
        result = convert_fromiso_to_unix_timestamp("2022-10-19T16:34:25.041Z")
        self.assertAlmostEqual(1666197265041.0, result, delta=1.0)

    def test_convert_fromiso_to_unix_timestamp_with_offset(self):
        result = convert_fromiso_to_unix_timestamp("2022-10-19T16:34:25.041+00:00")
        self.assertAlmostEqual(1666197265041.0, result, delta=1.0)

    def test_convert_fromiso_to_unix_timestamp_timezone_correctness(self):
        """Regression test for Fix 12: output must be UTC regardless of local timezone."""
        result_z = convert_fromiso_to_unix_timestamp("2022-10-19T16:34:25.041Z")
        result_offset = convert_fromiso_to_unix_timestamp("2022-10-19T16:34:25.041+00:00")

        # Both must produce the same value
        self.assertAlmostEqual(result_z, result_offset, delta=1.0)

        # And both must equal the known UTC epoch value in milliseconds
        # 2022-10-19T16:34:25.041Z = 1666197265.041 seconds since epoch
        expected_ms = 1666197265041.0
        self.assertAlmostEqual(expected_ms, result_z, delta=1.0)
        self.assertAlmostEqual(expected_ms, result_offset, delta=1.0)

    def test_is_exchange_information_valid_active(self):
        self.assertTrue(is_market_active({"isActive": True}))
        self.assertTrue(is_market_active({"active": True}))

    def test_is_exchange_information_valid_inactive(self):
        self.assertFalse(is_market_active({"isActive": False}))
        self.assertFalse(is_market_active({"active": False}))
        self.assertFalse(is_market_active({}))

    def test_example_pair_is_valid(self):
        """EXAMPLE_PAIR must be a real pair on the exchange, not the old ZRX-ETH."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_utils import EXAMPLE_PAIR
        self.assertEqual("BTC-USDT", EXAMPLE_PAIR)
        self.assertIn("-", EXAMPLE_PAIR)
        self.assertNotIn("/", EXAMPLE_PAIR)

    def test_default_fees_are_0_0015(self):
        """Phase 5A: Verify fee defaults updated from 0.001 to 0.0015."""
        from decimal import Decimal
        from hummingbot.connector.exchange.nonkyc.nonkyc_utils import DEFAULT_FEES
        self.assertEqual(DEFAULT_FEES.maker_percent_fee_decimal, Decimal("0.0015"))
        self.assertEqual(DEFAULT_FEES.taker_percent_fee_decimal, Decimal("0.0015"))
        self.assertTrue(DEFAULT_FEES.buy_percent_fee_deducted_from_returns)
