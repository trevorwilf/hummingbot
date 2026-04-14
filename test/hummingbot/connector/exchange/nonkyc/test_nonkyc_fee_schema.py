"""Tests for NonKYC fee schema correctness — BUY fees must be AddedToCost."""
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import (
    AddedToCostTradeFee,
    DeductedFromReturnsTradeFee,
    TradeFeeBase,
)


class TestNonkycFeeSchema(unittest.TestCase):
    """Verify the NonKYC fee schema produces correct fee types."""

    def test_schema_flag_is_false(self):
        """buy_percent_fee_deducted_from_returns must be False."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_utils import DEFAULT_FEES
        self.assertFalse(DEFAULT_FEES.buy_percent_fee_deducted_from_returns)

    def test_buy_fee_is_added_to_cost_via_schema(self):
        """BUY orders should produce AddedToCostTradeFee from the schema."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_utils import DEFAULT_FEES
        fee = TradeFeeBase.new_spot_fee(
            fee_schema=DEFAULT_FEES,
            trade_type=TradeType.BUY,
            percent=Decimal("0.002"),
        )
        self.assertIsInstance(fee, AddedToCostTradeFee)

    def test_sell_fee_is_deducted_from_returns_via_schema(self):
        """SELL orders should produce DeductedFromReturnsTradeFee from the schema."""
        from hummingbot.connector.exchange.nonkyc.nonkyc_utils import DEFAULT_FEES
        fee = TradeFeeBase.new_spot_fee(
            fee_schema=DEFAULT_FEES,
            trade_type=TradeType.SELL,
            percent=Decimal("0.002"),
        )
        self.assertIsInstance(fee, DeductedFromReturnsTradeFee)


class TestNonkycConnectorGetFee(unittest.TestCase):
    """Verify the connector _get_fee returns correct types per trade side."""

    def _make_exchange(self):
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
        exchange = MagicMock(spec=NonkycExchange)
        exchange._trading_fees = {"maker_fee": Decimal("0.001"), "taker_fee": Decimal("0.002")}
        exchange.estimate_fee_pct = MagicMock(return_value=Decimal("0.002"))
        exchange._get_fee = NonkycExchange._get_fee.__get__(exchange, NonkycExchange)
        return exchange

    def test_buy_returns_added_to_cost(self):
        exchange = self._make_exchange()
        fee = exchange._get_fee("ARRR", "USDT", OrderType.LIMIT, TradeType.BUY, Decimal("10"))
        self.assertIsInstance(fee, AddedToCostTradeFee)

    def test_sell_returns_deducted_from_returns(self):
        exchange = self._make_exchange()
        fee = exchange._get_fee("ARRR", "USDT", OrderType.LIMIT, TradeType.SELL, Decimal("10"))
        self.assertIsInstance(fee, DeductedFromReturnsTradeFee)


if __name__ == "__main__":
    unittest.main()
