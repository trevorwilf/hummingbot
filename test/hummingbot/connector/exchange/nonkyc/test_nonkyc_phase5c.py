"""Phase 5C: Dynamic Fee System unit tests."""
import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, patch

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee


class TestPhase5CDynamicFees(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        super().setUp()
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )

    def test_trading_fees_dict_initialized_empty(self):
        """Phase 5C: _trading_fees should be an empty dict on init."""
        self.assertIsInstance(self.exchange._trading_fees, dict)
        self.assertEqual(0, len(self.exchange._trading_fees))

    def test_get_fee_falls_back_to_defaults_when_no_computed_rates(self):
        """Phase 5C: _get_fee should use static defaults when _trading_fees is empty."""
        fee = self.exchange._get_fee("BTC", "USDT", OrderType.LIMIT, TradeType.BUY, Decimal("1"))
        self.assertIsInstance(fee, DeductedFromReturnsTradeFee)
        # Should match the DEFAULT_FEES maker rate
        self.assertGreater(fee.percent, Decimal("0"))

    def test_get_fee_uses_computed_maker_rate(self):
        """Phase 5C: _get_fee should return computed maker rate when available."""
        self.exchange._trading_fees = {
            "maker_fee": Decimal("0.002"),
            "taker_fee": Decimal("0.003"),
        }
        fee = self.exchange._get_fee(
            "BTC", "USDT", OrderType.LIMIT_MAKER, TradeType.BUY, Decimal("1"))
        self.assertEqual(Decimal("0.002"), fee.percent)

    def test_get_fee_uses_computed_taker_rate(self):
        """Phase 5C: _get_fee should return computed taker rate for non-maker orders."""
        self.exchange._trading_fees = {
            "maker_fee": Decimal("0.002"),
            "taker_fee": Decimal("0.003"),
        }
        fee = self.exchange._get_fee(
            "BTC", "USDT", OrderType.LIMIT, TradeType.BUY, Decimal("1"))
        self.assertEqual(Decimal("0.003"), fee.percent)

    def test_get_fee_respects_explicit_is_maker_flag(self):
        """Phase 5C: _get_fee should respect explicit is_maker=True even for LIMIT orders."""
        self.exchange._trading_fees = {
            "maker_fee": Decimal("0.001"),
            "taker_fee": Decimal("0.005"),
        }
        fee = self.exchange._get_fee(
            "BTC", "USDT", OrderType.LIMIT, TradeType.BUY, Decimal("1"),
            is_maker=True)
        self.assertEqual(Decimal("0.001"), fee.percent)

    def test_get_fee_partial_cache_falls_back(self):
        """Phase 5C: If only taker is cached, maker should fall back to defaults."""
        self.exchange._trading_fees = {"taker_fee": Decimal("0.003")}
        fee = self.exchange._get_fee(
            "BTC", "USDT", OrderType.LIMIT_MAKER, TradeType.BUY, Decimal("1"))
        # maker_fee not in cache -> should fall back to estimate_fee_pct
        # The fee should be the static default, not the taker rate
        self.assertNotEqual(Decimal("0.003"), fee.percent)

    async def test_update_trading_fees_computes_from_trades(self):
        """Phase 5C: _update_trading_fees should compute rates from trade history."""
        mock_trades = [
            {
                "side": "Buy", "triggeredBy": "sell",  # maker
                "fee": "0.002", "quantity": "10", "price": "0.1",
            },
            {
                "side": "Sell", "triggeredBy": "sell",  # taker
                "fee": "0.003", "quantity": "10", "price": "0.1",
            },
        ]
        with patch.object(self.exchange, '_api_get', new_callable=AsyncMock, return_value=mock_trades):
            await self.exchange._update_trading_fees()

        self.assertIn("maker_fee", self.exchange._trading_fees)
        self.assertIn("taker_fee", self.exchange._trading_fees)
        # maker: fee=0.002, notional=1.0 -> rate=0.002
        self.assertEqual(Decimal("0.002"), self.exchange._trading_fees["maker_fee"])
        # taker: fee=0.003, notional=1.0 -> rate=0.003
        self.assertEqual(Decimal("0.003"), self.exchange._trading_fees["taker_fee"])

    async def test_update_trading_fees_skips_zero_fee_trades(self):
        """Phase 5C: Trades with zero fee should be excluded from rate calculation."""
        mock_trades = [
            {
                "side": "Buy", "triggeredBy": "sell",  # maker
                "fee": "0.00000000", "quantity": "10", "price": "0.1",
            },
            {
                "side": "Buy", "triggeredBy": "sell",  # maker
                "fee": "0.001", "quantity": "10", "price": "0.1",
            },
        ]
        with patch.object(self.exchange, '_api_get', new_callable=AsyncMock, return_value=mock_trades):
            await self.exchange._update_trading_fees()

        # Only the non-zero trade should be used
        self.assertEqual(Decimal("0.001"), self.exchange._trading_fees["maker_fee"])

    async def test_update_trading_fees_empty_history_keeps_empty_cache(self):
        """Phase 5C: Empty trade history should not populate _trading_fees."""
        with patch.object(self.exchange, '_api_get', new_callable=AsyncMock, return_value=[]):
            await self.exchange._update_trading_fees()

        self.assertEqual(0, len(self.exchange._trading_fees))

    async def test_update_trading_fees_handles_api_error_gracefully(self):
        """Phase 5C: API errors should be caught without crashing."""
        with patch.object(self.exchange, '_api_get', new_callable=AsyncMock,
                          side_effect=Exception("Connection error")):
            # Should not raise
            await self.exchange._update_trading_fees()

        self.assertEqual(0, len(self.exchange._trading_fees))

    async def test_update_trading_fees_averages_multiple_trades(self):
        """Phase 5C: Multiple trades should be averaged per maker/taker category."""
        mock_trades = [
            {"side": "Buy", "triggeredBy": "sell", "fee": "0.002", "quantity": "10", "price": "0.1"},
            {"side": "Sell", "triggeredBy": "buy", "fee": "0.004", "quantity": "10", "price": "0.1"},
            # Two maker trades: rates 0.002 and 0.004 -> average 0.003
        ]
        with patch.object(self.exchange, '_api_get', new_callable=AsyncMock, return_value=mock_trades):
            await self.exchange._update_trading_fees()

        # Average of 0.002 and 0.004 = 0.003
        self.assertEqual(Decimal("0.003"), self.exchange._trading_fees["maker_fee"])
