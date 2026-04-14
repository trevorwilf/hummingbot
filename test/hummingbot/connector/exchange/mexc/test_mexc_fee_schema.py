"""Tests for MEXC fee schema correctness — BUY fees must be AddedToCost."""
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import (
    AddedToCostTradeFee,
    DeductedFromReturnsTradeFee,
    TradeFeeBase,
)


class TestMexcFeeSchema(unittest.TestCase):
    def test_schema_flag_is_false(self):
        from hummingbot.connector.exchange.mexc.mexc_utils import DEFAULT_FEES
        self.assertFalse(DEFAULT_FEES.buy_percent_fee_deducted_from_returns)

    def test_buy_fee_is_added_to_cost_via_schema(self):
        from hummingbot.connector.exchange.mexc.mexc_utils import DEFAULT_FEES
        fee = TradeFeeBase.new_spot_fee(
            fee_schema=DEFAULT_FEES,
            trade_type=TradeType.BUY,
            percent=Decimal("0.0005"),
        )
        self.assertIsInstance(fee, AddedToCostTradeFee)

    def test_sell_fee_is_deducted_from_returns_via_schema(self):
        from hummingbot.connector.exchange.mexc.mexc_utils import DEFAULT_FEES
        fee = TradeFeeBase.new_spot_fee(
            fee_schema=DEFAULT_FEES,
            trade_type=TradeType.SELL,
            percent=Decimal("0.0005"),
        )
        self.assertIsInstance(fee, DeductedFromReturnsTradeFee)


class TestMexcConnectorGetFee(unittest.TestCase):
    def _make_exchange(self):
        from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange
        exchange = MagicMock(spec=MexcExchange)
        exchange._trading_fees = {
            "RENDER-USDT": {"maker": Decimal("0.0005"), "taker": Decimal("0.0005")}
        }
        exchange.estimate_fee_pct = MagicMock(return_value=Decimal("0.0005"))
        exchange._get_fee = MexcExchange._get_fee.__get__(exchange, MexcExchange)
        return exchange

    def test_buy_returns_added_to_cost(self):
        exchange = self._make_exchange()
        fee = exchange._get_fee("RENDER", "USDT", OrderType.LIMIT, TradeType.BUY, Decimal("1"))
        self.assertIsInstance(fee, AddedToCostTradeFee)

    def test_sell_returns_deducted_from_returns(self):
        exchange = self._make_exchange()
        fee = exchange._get_fee("RENDER", "USDT", OrderType.LIMIT, TradeType.SELL, Decimal("1"))
        self.assertIsInstance(fee, DeductedFromReturnsTradeFee)


if __name__ == "__main__":
    unittest.main()
