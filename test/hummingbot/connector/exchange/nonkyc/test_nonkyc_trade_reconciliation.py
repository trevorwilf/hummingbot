"""Trade-poll reconciliation contract tests for the NonKYC connector.

/account/trades returns the GLOBAL account trade list (the symbol filter is ignored), so the fill
reconciliation path must (a) attribute each trade to its OWN order/market -- never the poll pair --
and (b) dedupe by exchange trade id (persisted across restarts). These tests prove the cross-pair
fill leak and the duplicate-fill re-insert are fixed.
"""
import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from bidict import bidict

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.events import MarketEvent

# exchange symbol ("BASE/QUOTE") -> hb pair ("BASE-QUOTE")
SYMBOLS = {
    "ZANO/USDT": "ZANO-USDT",
    "XMR/USDT": "XMR-USDT",
    "DASH/USDT": "DASH-USDT",
    "SUN/USDT": "SUN-USDT",
}


def _trade(tid, orderid, symbol, side="Buy", price="10", qty="1"):
    return {
        "id": tid, "orderid": orderid, "market": {"symbol": symbol}, "side": side,
        "triggeredBy": side, "price": price, "quantity": qty, "timestamp": 1700000000000, "fee": "0",
    }


class NonkycTradeReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ex = self._make_exchange()
        self.filled = EventLogger()
        self.ex.add_listener(MarketEvent.OrderFilled, self.filled)

    def tearDown(self):
        self.loop.close()

    @staticmethod
    def _make_exchange():
        ex = NonkycExchange(nonkyc_api_key="k", nonkyc_api_secret="s",
                            trading_pairs=list(SYMBOLS.values()), trading_required=False)
        ex._set_trading_pair_symbol_map(bidict(SYMBOLS))
        ex._set_current_timestamp(1_000_000_000)
        ex._last_poll_timestamp = 0.0
        return ex

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _drain_events(self):
        # Let any event scheduled on the loop fire before asserting.
        self._run(asyncio.sleep(0.05))

    def _track(self, oid, eid, pair, side=TradeType.BUY):
        self.ex.start_tracking_order(
            order_id=oid, exchange_order_id=eid, trading_pair=pair,
            order_type=OrderType.LIMIT, trade_type=side, price=Decimal("10"), amount=Decimal("5"))

    # ---- Test 1: cross-pair rejection ----------------------------------------------------------

    def test_cross_pair_rejection_only_owning_order_fills(self):
        # Only the DASH order is tracked. The global list also carries ZANO/XMR/SUN trades.
        self._track("c-dash", "dash-oid", "DASH-USDT")
        trades = [
            _trade("t-zano", "zano-oid", "ZANO/USDT"),
            _trade("t-xmr", "xmr-oid", "XMR/USDT"),
            _trade("t-dash", "dash-oid", "DASH/USDT"),
            _trade("t-sun", "sun-oid", "SUN/USDT"),
        ]
        with patch.object(self.ex, "_api_get", new=AsyncMock(return_value=trades)):
            self._run(self.ex._update_order_fills_from_trades())
        self._drain_events()

        pairs = [e.trading_pair for e in self.filled.event_log]
        self.assertEqual(["DASH-USDT"], pairs, "only the DASH trade may produce a fill")
        # And nothing leaked to the other three pairs.
        self.assertNotIn("ZANO-USDT", pairs)
        self.assertNotIn("XMR-USDT", pairs)
        self.assertNotIn("SUN-USDT", pairs)

    def test_recovery_path_no_cross_market_fanout(self):
        # An untracked-but-DB-known ZANO order: the global list is polled once per pair (4x) but the
        # trade must be recreated ONCE, for ZANO-USDT only -- never fanned across markets.
        self.ex._exchange_order_ids["zano-oid"] = "client-zano"
        trades = [_trade("t-zano", "zano-oid", "ZANO/USDT")]
        with patch.object(self.ex, "_api_get", new=AsyncMock(return_value=trades)):
            self._run(self.ex._update_order_fills_from_trades())
        self._drain_events()

        evs = self.filled.event_log
        self.assertEqual(1, len(evs), "recovery must fire exactly one fill, not one-per-market")
        self.assertEqual("ZANO-USDT", evs[0].trading_pair)

    # ---- Test 2: dedupe ------------------------------------------------------------------------

    def test_dedupe_same_trade_across_two_cycles(self):
        self._track("c-dash", "dash-oid", "DASH-USDT")
        trades = [_trade("t-dash", "dash-oid", "DASH/USDT")]
        api = AsyncMock(return_value=trades)
        with patch.object(self.ex, "_api_get", new=api):
            self._run(self.ex._update_order_fills_from_trades())
            self._drain_events()
            self.assertTrue(self.ex._is_trade_processed("t-dash"))
            self.ex._set_current_timestamp(2_000_000_000)  # next poll cycle
            self._run(self.ex._update_order_fills_from_trades())
            self._drain_events()

        self.assertEqual(1, len(self.filled.event_log), "second cycle must be a no-op")

    # ---- Test 3: dedupe persistence across restart ---------------------------------------------

    def test_dedupe_set_survives_restart(self):
        for tid in ("a", "b", "c"):
            self.ex._mark_trade_processed(tid)
        states = self.ex.tracking_states
        self.assertIn(self.ex._PROCESSED_TRADE_IDS_STATE_KEY, states)

        # New process: restore from the persisted states.
        ex2 = self._make_exchange()
        ex2.restore_tracking_states(states)
        for tid in ("a", "b", "c"):
            self.assertTrue(ex2._is_trade_processed(tid), f"{tid} must survive restart")
        # Restoring must NOT have leaked the reserved key into the order tracker.
        self.assertNotIn(self.ex._PROCESSED_TRADE_IDS_STATE_KEY, ex2._order_tracker.all_updatable_orders)

        # A previously-seen trade is skipped after restart (no new fill).
        ex2.add_listener(MarketEvent.OrderFilled, self.filled)
        ex2._exchange_order_ids["zano-oid"] = "client-zano"
        seen = [{"id": "a", "orderid": "zano-oid", "market": {"symbol": "ZANO/USDT"},
                 "side": "Buy", "triggeredBy": "Buy", "price": "10", "quantity": "1",
                 "timestamp": 1700000000000, "fee": "0"}]
        with patch.object(ex2, "_api_get", new=AsyncMock(return_value=seen)):
            self._run(ex2._update_order_fills_from_trades())
        self._run(asyncio.sleep(0.05))
        self.assertEqual(0, len(self.filled.event_log), "a previously-processed trade must be skipped")

    def test_mark_trade_processed_is_fifo_bounded(self):
        cap = self.ex._PROCESSED_TRADE_IDS_MAX
        for i in range(cap + 25):
            self.ex._mark_trade_processed(f"id-{i}")
        self.assertEqual(cap, len(self.ex._processed_trade_ids))
        self.assertFalse(self.ex._is_trade_processed("id-0"), "oldest evicted")
        self.assertTrue(self.ex._is_trade_processed(f"id-{cap + 24}"), "newest retained")


if __name__ == "__main__":
    unittest.main()
