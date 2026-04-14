"""Tests for MEXC local balance pre-adjust after order acceptance."""
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.core.data_type.common import OrderType, TradeType


class TestMexcBalancePreAdjust(unittest.IsolatedAsyncioTestCase):
    """Verify MEXC locally deducts balance after order acceptance."""

    async def _make_exchange_and_order(self, trade_type, amount, price, base_avail, quote_avail):
        from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange

        exchange = MagicMock(spec=MexcExchange)
        exchange._account_available_balances = {
            "RENDER": base_avail,
            "USDT": quote_avail,
        }
        exchange.estimate_fee_pct = MagicMock(return_value=Decimal("0.0005"))
        exchange.logger = MagicMock(return_value=MagicMock())

        order = MagicMock()
        order.trading_pair = "RENDER-USDT"
        order.trade_type = trade_type
        order.amount = amount
        order.price = price
        order.client_order_id = "test_order_1"

        return exchange, order

    async def test_sell_deducts_base_balance(self):
        exchange, order = await self._make_exchange_and_order(
            TradeType.SELL, Decimal("5.0"), Decimal("1.9"),
            base_avail=Decimal("10.0"), quote_avail=Decimal("100.0")
        )

        from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange

        with patch.object(
            MexcExchange.__bases__[0], '_place_order_and_process_update',
            new_callable=AsyncMock, return_value="exchange_123"
        ):
            result = await MexcExchange._place_order_and_process_update(exchange, order)

        self.assertEqual(result, "exchange_123")
        self.assertEqual(exchange._account_available_balances["RENDER"], Decimal("5.0"))
        self.assertEqual(exchange._account_available_balances["USDT"], Decimal("100.0"))

    async def test_buy_deducts_quote_balance_with_fee(self):
        exchange, order = await self._make_exchange_and_order(
            TradeType.BUY, Decimal("5.0"), Decimal("2.0"),
            base_avail=Decimal("10.0"), quote_avail=Decimal("100.0")
        )

        from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange

        with patch.object(
            MexcExchange.__bases__[0], '_place_order_and_process_update',
            new_callable=AsyncMock, return_value="exchange_456"
        ):
            result = await MexcExchange._place_order_and_process_update(exchange, order)

        self.assertEqual(result, "exchange_456")
        # notional = 5 * 2 = 10, fee = 10 * 0.0005 = 0.005, hold = 10.005
        expected_quote = Decimal("100.0") - Decimal("10.005")
        self.assertEqual(exchange._account_available_balances["USDT"], expected_quote)

    async def test_balance_does_not_go_negative(self):
        exchange, order = await self._make_exchange_and_order(
            TradeType.SELL, Decimal("15.0"), Decimal("1.9"),
            base_avail=Decimal("10.0"), quote_avail=Decimal("100.0")
        )

        from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange

        with patch.object(
            MexcExchange.__bases__[0], '_place_order_and_process_update',
            new_callable=AsyncMock, return_value="exchange_789"
        ):
            result = await MexcExchange._place_order_and_process_update(exchange, order)

        self.assertEqual(exchange._account_available_balances["RENDER"], Decimal("0"))


if __name__ == "__main__":
    unittest.main()
