"""Phase 9 hardening (controller items).

9d: _mark_bypass_cooldown_for_level derives the bypass duration from the LEVEL'S SIDE
    (effective_buy_cooldown_time / effective_sell_cooldown_time; unknown prefix -> max of
    the two) instead of the flat cooldown_time + 1, so a legacy-mode bypass can no longer
    expire before the side's real cooldown would have.
9e: the state path is resolved to an absolute path once at first use and logged on
    range_ladder_live_run_started (state_file_abs); the first _save_state writes a sidecar
    <state>.owner marker (controller id + PID + start timestamp) and a marker owned by a
    DIFFERENT controller id emits range_ladder_state_file_contention (warn-only).
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from hummingbot.core.data_type.common import OrderType, PriceType  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _make_mdp(*, balances=None, mid=335, bid=334.9, ask=335.1, now=1000.0):
    balances = balances if balances is not None else {"XMR": (D(0), D(0)), "USDT": (D(100), D(100))}
    mdp = MagicMock()
    mdp.time.return_value = now
    mdp.get_price_by_type.side_effect = lambda c, p, pt: {
        PriceType.MidPrice: D(mid), PriceType.BestBid: D(bid), PriceType.BestAsk: D(ask)}[pt]
    mdp.get_balance.side_effect = lambda c, a: balances[a][0]
    mdp.get_available_balance.side_effect = lambda c, a: balances[a][1]
    mdp.quantize_order_price.side_effect = lambda c, p, price: D(price)
    mdp.quantize_order_amount.side_effect = lambda c, p, amt: D(amt).quantize(D("0.000001"), rounding=ROUND_DOWN)
    connector = MagicMock()
    connector.supported_order_types.return_value = [OrderType.LIMIT_MAKER, OrderType.LIMIT]
    connector.in_flight_orders = {}
    mdp.get_connector.return_value = connector
    return mdp


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "state.json"

    def _build(self, mdp=None, *, cid="ctrl-p9", **config_overrides):
        mdp = mdp or _make_mdp()
        defaults = dict(
            id=cid,
            controller_name="range_inventory_ladder",
            controller_type="market_making",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("100"),
            buy_prices=[Decimal("321")],
            buy_amounts_pct=[Decimal("1")],
            sell_prices=[Decimal("350")],
            sell_amounts_pct=[Decimal("1")],
        )
        defaults.update(config_overrides)
        config = RangeInventoryLadderConfig(**defaults)
        ctrl = RangeInventoryLadderController(
            config, market_data_provider=mdp, actions_queue=MagicMock()
        )
        ctrl._emit_structured = MagicMock()
        patcher = patch.object(
            type(ctrl), "state_path", new_callable=PropertyMock,
            return_value=self.state_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        ctrl.executors_info = []
        ctrl.positions_held = []
        return ctrl

    @staticmethod
    def _events(ctrl, event_type):
        return [c for c in ctrl._emit_structured.call_args_list if c.args and c.args[0] == event_type]


# =========================================================================
# 9d: per-side bypass cooldown duration
# =========================================================================

class TestPerSideBypassDuration(_Harness):

    def test_buy_bypass_outlives_longer_buy_cooldown(self):
        ctrl = self._build(buy_cooldown_time=7200, cooldown_time=3600)
        ctrl._mark_bypass_cooldown_for_level("buy_321")
        expiry = ctrl._cooldown_bypass_until_by_level["buy_321"]
        self.assertGreater(expiry - 1000.0, 7200)   # lasts > the side's REAL cooldown
        self.assertEqual(1000.0 + 7201, expiry)

    def test_sell_bypass_uses_sell_side_cooldown(self):
        ctrl = self._build(buy_cooldown_time=7200, cooldown_time=3600)
        ctrl._mark_bypass_cooldown_for_level("sell_350")
        # sell_cooldown_time unset -> effective sell cooldown falls back to cooldown_time.
        self.assertEqual(1000.0 + 3601, ctrl._cooldown_bypass_until_by_level["sell_350"])

    def test_unknown_prefix_takes_max_of_both_sides(self):
        ctrl = self._build(buy_cooldown_time=7200, sell_cooldown_time=1800, cooldown_time=3600)
        ctrl._mark_bypass_cooldown_for_level("weird_level")
        self.assertEqual(1000.0 + 7201, ctrl._cooldown_bypass_until_by_level["weird_level"])

    def test_default_config_matches_legacy_duration(self):
        # Without per-side overrides both sides fall back to cooldown_time -> old behavior.
        ctrl = self._build(cooldown_time=3600)
        ctrl._mark_bypass_cooldown_for_level("buy_321")
        self.assertEqual(1000.0 + 3601, ctrl._cooldown_bypass_until_by_level["buy_321"])


# =========================================================================
# 9e: state-path anchoring + owner marker
# =========================================================================

class TestStateOwnerMarker(_Harness):

    def _saved(self, ctrl):
        ctrl._state = {"initialized": True, "owned_quote": "1"}
        ctrl._save_state()
        return ctrl

    def test_first_save_writes_owner_marker(self):
        self._saved(self._build(cid="ctrl-a"))
        marker = Path(f"{self.state_path}.owner")
        self.assertTrue(marker.exists())
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual("ctrl-a", payload["controller_id"])
        self.assertEqual(os.getpid(), payload["pid"])
        self.assertIn("started_at", payload)

    def test_different_controller_id_emits_contention_event(self):
        self._saved(self._build(cid="ctrl-a"))
        intruder = self._saved(self._build(cid="ctrl-b"))
        events = self._events(intruder, "range_ladder_state_file_contention")
        self.assertEqual(1, len(events))
        self.assertEqual("ctrl-a", events[0].kwargs["marker_controller_id"])
        self.assertEqual(str(self.state_path), events[0].kwargs["state_file"])
        # warn-only: the save still happened and the intruder took over the marker
        self.assertTrue(self.state_path.exists())
        payload = json.loads(Path(f"{self.state_path}.owner").read_text(encoding="utf-8"))
        self.assertEqual("ctrl-b", payload["controller_id"])

    def test_same_controller_id_does_not_emit(self):
        self._saved(self._build(cid="ctrl-a"))
        again = self._saved(self._build(cid="ctrl-a"))
        self.assertEqual([], self._events(again, "range_ladder_state_file_contention"))

    def test_marker_checked_once_per_process(self):
        ctrl = self._saved(self._build(cid="ctrl-a"))
        # Plant a foreign marker AFTER the first save: subsequent saves must not re-check.
        Path(f"{self.state_path}.owner").write_text(
            json.dumps({"controller_id": "ctrl-x", "pid": 1, "started_at": 0}), encoding="utf-8")
        ctrl._save_state()
        self.assertEqual([], self._events(ctrl, "range_ladder_state_file_contention"))

    def test_live_run_started_includes_state_file_abs(self):
        ctrl = self._build(cid="ctrl-a")
        ctrl._state = {
            "initialized": True,
            "owned_quote": "100", "owned_base": "0", "seed_value_quote": "100",
            "initial_managed_quote": "100", "initial_claimed_base_amount": "0",
            "initial_reference_price": "335",
            "reserve_quote_balance": "0", "reserve_base_balance": "0",
            "tracked_fill_executor_ids": [],
        }
        ctrl._state_loaded = True
        asyncio.run(ctrl.update_processed_data())
        started = self._events(ctrl, "range_ladder_live_run_started")
        self.assertEqual(1, len(started))
        self.assertEqual(str(self.state_path.resolve()), started[0].kwargs["state_file_abs"])

    def test_state_path_abs_resolved_once_and_cached(self):
        ctrl = self._build(cid="ctrl-a")
        first = ctrl.state_path_abs
        self.assertTrue(first.is_absolute())
        self.assertIs(first, ctrl.state_path_abs)  # cached, not re-resolved


if __name__ == "__main__":
    unittest.main()
