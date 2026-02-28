"""
Phase 5A unit tests -- LIMIT_MAKER, Decimal precision, cancel_all_orders constant.
"""
import asyncio
import unittest
from decimal import Decimal

from hummingbot.core.data_type.common import OrderType


class TestPhase5AOrderTypes(unittest.TestCase):
    """Test LIMIT_MAKER order type mapping and supported types."""

    def test_limit_maker_maps_to_limit(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
        self.assertEqual(NonkycExchange.nonkyc_order_type(OrderType.LIMIT_MAKER), "limit")

    def test_limit_maps_to_limit(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
        self.assertEqual(NonkycExchange.nonkyc_order_type(OrderType.LIMIT), "limit")

    def test_market_maps_to_market(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
        self.assertEqual(NonkycExchange.nonkyc_order_type(OrderType.MARKET), "market")

    def test_supported_types_includes_limit_maker(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
        exchange = NonkycExchange(
            nonkyc_api_key="test", nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"], trading_required=False)
        self.assertIn(OrderType.LIMIT_MAKER, exchange.supported_order_types())


class TestPhase5ADecimalPrecision(unittest.TestCase):
    """Test that Decimal precision fix eliminates float noise."""

    def test_old_formula_has_float_noise(self):
        """Prove the bug existed: Decimal(1 / (10**8)) != Decimal('1E-8')."""
        buggy = Decimal(1 / (10 ** 8))
        correct = Decimal(10) ** Decimal(-8)
        self.assertNotEqual(buggy, correct)  # They should NOT be equal -- proves the bug

    def test_new_formula_is_exact(self):
        """Verify fix: Decimal(10) ** (-int(d)) gives exact result."""
        for decimals in [2, 4, 6, 8, 10]:
            result = Decimal(10) ** (-int(decimals))
            expected = Decimal("1E-{}".format(decimals))
            self.assertEqual(result, expected,
                             "Decimal(10)**(-{}) should be 1E-{}, got {}".format(
                                 decimals, decimals, result))


class TestPhase5AConstants(unittest.TestCase):
    """Test new constants added in Phase 5A."""

    def test_cancel_all_orders_path_url(self):
        from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
        self.assertEqual(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL, "/cancelallorders")

    def test_cancel_all_has_rate_limit(self):
        from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
        limit_ids = [rl.limit_id for rl in CONSTANTS.RATE_LIMITS]
        self.assertIn(CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL, limit_ids)
