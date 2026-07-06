"""Phase 9 hardening (connector items).

9a: WS trade reports derive maker/taker from side vs triggeredBy (like the REST poll path)
    instead of hardcoding is_taker=True, in both the TradeUpdate and the
    connector_fill_received structured event; missing fields default to taker.
9b: /getorder (ORDER_INFO_PATH_URL) no longer draws from the ORDERS / ORDERS_24HR throttle
    pools, so status polling cannot starve create/cancel capacity during refresh waves.
9c: the FIRST fill poll passes since = now - 3 days instead of omitting `since`, so an old
    account doesn't pull its entire global trade history at startup; subsequent polls keep
    using the tracked timestamp.
"""
import asyncio
import unittest
from decimal import Decimal
from typing import Awaitable
from unittest.mock import AsyncMock, MagicMock, patch

from bidict import bidict

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType

THREE_DAYS_S = 3 * 24 * 3600


class _Base(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.exchange = NonkycExchange(
            nonkyc_api_key="k", nonkyc_api_secret="s",
            trading_pairs=["BTC-USDT"], trading_required=False,
        )
        self.exchange._set_trading_pair_symbol_map(bidict({"BTC/USDT": "BTC-USDT"}))

    def tearDown(self):
        self.loop.close()

    def async_run(self, coro: Awaitable):
        return self.loop.run_until_complete(coro)


# =========================================================================
# 9a: WS trade reports derive maker/taker
# =========================================================================

class TestWsReportMakerTaker(_Base):

    def _report(self, **param_overrides):
        params = {
            "reportType": "trade",
            "userProvidedId": "HBOT-9A",
            "tradeId": 777,
            "id": "EXCH-9A",
            "symbol": "BTC/USDT",
            "tradeQuantity": "0.1",
            "tradePrice": "50000",
            "updatedAt": 1700000000000,
            "status": "Active",
            "fee": "0.5",
        }
        params.update(param_overrides)
        return {"method": "report", "params": params}

    def _run_listener_with(self, event, sel_mock):
        self.exchange.start_tracking_order(
            order_id="HBOT-9A", exchange_order_id="EXCH-9A", trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT, trade_type=TradeType.SELL,
            price=Decimal("50000"), amount=Decimal("0.1"))
        captured = {}

        def capture_trade_update(trade_update):
            captured["trade_update"] = trade_update

        async def run():
            queue = asyncio.Queue()
            queue.put_nowait(event)

            async def mock_iter():
                while not queue.empty():
                    yield queue.get_nowait()

            with patch.object(self.exchange, "_iter_user_event_queue", mock_iter), \
                    patch.object(self.exchange._order_tracker, "process_trade_update",
                                 side_effect=capture_trade_update), \
                    patch.object(self.exchange, "get_order_book", return_value=None), \
                    patch("hummingbot.logger.structured_event_logger.get_structured_logger",
                          return_value=sel_mock):
                await self.exchange._user_stream_event_listener()

        self.async_run(run())
        return captured.get("trade_update")

    @staticmethod
    def _fill_events(sel_mock):
        return [c for c in sel_mock.emit.call_args_list
                if c.args and c.args[0] == "connector_fill_received"]

    def test_maker_signature_ws_report_is_taker_false(self):
        # A SELL order filled because a BUY triggered the trade -> we were the maker.
        sel = MagicMock()
        tu = self._run_listener_with(self._report(side="sell", triggeredBy="buy"), sel)
        self.assertIsNotNone(tu)
        self.assertFalse(tu.is_taker)
        fills = self._fill_events(sel)
        self.assertEqual(1, len(fills))
        self.assertFalse(fills[0].kwargs["is_taker"])

    def test_taker_signature_ws_report_is_taker_true(self):
        sel = MagicMock()
        tu = self._run_listener_with(self._report(side="sell", triggeredBy="sell"), sel)
        self.assertIsNotNone(tu)
        self.assertTrue(tu.is_taker)
        self.assertTrue(self._fill_events(sel)[0].kwargs["is_taker"])

    def test_missing_fields_default_to_taker(self):
        sel = MagicMock()
        tu = self._run_listener_with(self._report(), sel)  # no side / triggeredBy keys
        self.assertIsNotNone(tu)
        self.assertTrue(tu.is_taker)
        self.assertTrue(self._fill_events(sel)[0].kwargs["is_taker"])


# =========================================================================
# 9b: /getorder out of the ORDERS throttle pools
# =========================================================================

class TestOrderInfoThrottlePools(unittest.TestCase):

    @staticmethod
    def _linked_ids(limit_id):
        limit = next(rl for rl in CONSTANTS.RATE_LIMITS if rl.limit_id == limit_id)
        return {pair.limit_id for pair in limit.linked_limits}

    def test_order_info_has_no_orders_pool_links(self):
        linked = self._linked_ids(CONSTANTS.ORDER_INFO_PATH_URL)
        self.assertNotIn(CONSTANTS.ORDERS, linked)
        self.assertNotIn(CONSTANTS.ORDERS_24HR, linked)
        self.assertIn(CONSTANTS.REQUEST_WEIGHT, linked)
        self.assertIn(CONSTANTS.RAW_REQUESTS, linked)

    def test_create_and_cancel_still_draw_from_orders_pools(self):
        for limit_id in (CONSTANTS.CREATE_ORDER_PATH_URL,
                         CONSTANTS.CANCEL_ORDER_PATH_URL,
                         CONSTANTS.CANCEL_ALL_ORDERS_PATH_URL):
            linked = self._linked_ids(limit_id)
            self.assertIn(CONSTANTS.ORDERS, linked, limit_id)
            self.assertIn(CONSTANTS.ORDERS_24HR, linked, limit_id)


# =========================================================================
# 9c: first-poll `since` floor
# =========================================================================

class TestFirstPollSinceFloor(_Base):

    NOW = 1_700_000_000.0

    def setUp(self):
        super().setUp()
        self.exchange._set_current_timestamp(1_000_000_000)
        self.exchange._last_poll_timestamp = 0.0
        self.exchange._time_synchronizer = MagicMock()
        self.exchange._time_synchronizer.time.return_value = self.NOW

    def test_first_poll_includes_since_three_days_back(self):
        api = AsyncMock(return_value=[])
        with patch.object(self.exchange, "_api_get", new=api):
            self.async_run(self.exchange._update_order_fills_from_trades())

        self.assertEqual(1, api.await_count)
        params = api.call_args.kwargs["params"]
        self.assertIn("since", params)
        self.assertEqual(int((self.NOW - THREE_DAYS_S) * 1e3), params["since"])

    def test_second_poll_uses_tracked_timestamp(self):
        api = AsyncMock(return_value=[])
        with patch.object(self.exchange, "_api_get", new=api):
            self.async_run(self.exchange._update_order_fills_from_trades())
            # Simulate the normal post-poll bookkeeping done by _update_time_and_poll.
            self.exchange._last_poll_timestamp = 1_000_000_000
            self.exchange._set_current_timestamp(2_000_000_000)
            self.async_run(self.exchange._update_order_fills_from_trades())

        self.assertEqual(2, api.await_count)
        second_params = api.call_args.kwargs["params"]
        # The tracked poll timestamp (set to NOW during the first poll), not the 3-day floor.
        self.assertEqual(int(self.NOW * 1e3), second_params["since"])


if __name__ == "__main__":
    unittest.main()
