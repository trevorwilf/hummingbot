"""
Integration tests for range_inventory_ladder fill accounting.

These tests drive the REAL controller method
``RangeInventoryLadderController._update_ledger_from_completed_executors()``
with real ``ExecutorInfo`` objects, instead of re-implementing the arithmetic
inline. This is the test that would have caught the original bug: the ledger
never moved because ``ExecutorInfo.filled_amount_quote`` is hardcoded to 0 for
OrderExecutor, and the controller read only that field.

The fix exposes the executor's real fills via ``custom_info`` (Part A) and makes
the controller prefer those fields (Part B). The regression test below asserts a
fill with ``filled_amount_quote == 0`` on the public property but populated
``custom_info`` is STILL booked — reverting Part B makes it fail.
"""
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

_CONTROLLER_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))

from hummingbot.core.data_type.common import TradeType  # noqa: E402
from hummingbot.strategy_v2.executors.order_executor.data_types import (  # noqa: E402
    ExecutionStrategy,
    OrderExecutorConfig,
)
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402
from hummingbot.strategy_v2.models.executors import CloseType  # noqa: E402
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo  # noqa: E402

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)


class TestFillLedgerIntegration(unittest.TestCase):
    """Drive the real ledger-update path with real ExecutorInfo objects."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._state_path = Path(self._tmpdir.name) / "state.json"

    def _make_controller(self, owned_quote="100", owned_base="0.5", tracked_ids=None):
        config = RangeInventoryLadderConfig(
            id="ctrl-1",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=100,
            buy_prices=[321, 318, 315],
            buy_amounts_pct=[1, 1, 1],
            sell_prices=[350, 355, 360],
            sell_amounts_pct=[1, 1, 1],
        )
        controller = RangeInventoryLadderController(
            config, market_data_provider=MagicMock(), actions_queue=MagicMock()
        )
        # Initialized ledger state, persisted to a temp file via the state_path patch below.
        controller._state = {
            "initialized": True,
            "owned_quote": str(owned_quote),
            "owned_base": str(owned_base),
            "tracked_fill_executor_ids": list(tracked_ids or []),
        }
        controller._state_loaded = True
        # Capture structured events; this also avoids diagnostic file writes.
        controller._emit_structured = MagicMock()
        # Redirect persistence to a temp file so _save_state really runs (no repo data/ writes).
        patcher = patch.object(
            type(controller), "state_path", new_callable=PropertyMock,
            return_value=self._state_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return controller

    @staticmethod
    def _make_executor_info(
        executor_id,
        side,
        price,
        level_id="buy_0",
        is_active=False,
        custom_info=None,
        public_filled_quote="0",
        public_fees="0",
    ):
        config = OrderExecutorConfig(
            id=executor_id,
            timestamp=123.0,
            side=side,
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            amount=Decimal("1"),
            price=Decimal(str(price)),
            execution_strategy=ExecutionStrategy.LIMIT,
            level_id=level_id,
        )
        return ExecutorInfo(
            id=executor_id,
            timestamp=123.0,
            type="order_executor",
            status=RunnableStatus.TERMINATED,
            config=config,
            net_pnl_pct=Decimal("0"),
            net_pnl_quote=Decimal("0"),
            cum_fees_quote=Decimal(str(public_fees)),
            filled_amount_quote=Decimal(str(public_filled_quote)),
            is_active=is_active,
            is_trading=False,
            custom_info=custom_info if custom_info is not None else {},
            close_type=CloseType.POSITION_HOLD,
            controller_id="ctrl-1",
        )

    @staticmethod
    def _exact_fill_custom_info(side, filled_base, filled_quote, fees):
        return {
            "side": side,
            "filled_amount_base": Decimal(str(filled_base)),
            "filled_amount_quote": Decimal(str(filled_quote)),
            "cum_fees_quote": Decimal(str(fees)),
        }

    @staticmethod
    def _d(value):
        return Decimal(str(value))

    def _emit_count(self, controller, event_type):
        return sum(
            1 for call in controller._emit_structured.call_args_list
            if call.args and call.args[0] == event_type
        )

    # ------------------------------------------------------------------ SELL

    def test_sell_fill_books_quote_up_base_down(self):
        controller = self._make_controller(owned_quote="100", owned_base="0.5")
        info = self._exact_fill_custom_info(TradeType.SELL, "0.08", "27.72", "0.05")
        executor = self._make_executor_info(
            "EX-SELL", TradeType.SELL, price="350", level_id="sell_0", custom_info=info
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        # owned_quote += (filled_quote - fees); owned_base -= filled_base
        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("127.67"))
        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("0.42"))
        self.assertIn("EX-SELL", controller._state["booked_fill_progress"])
        self.assertEqual(self._emit_count(controller, "range_ladder_ledger_updated"), 1)

    # ------------------------------------------------------------------ BUY

    def test_buy_fill_books_quote_down_base_up(self):
        controller = self._make_controller(owned_quote="100", owned_base="0.5")
        info = self._exact_fill_custom_info(TradeType.BUY, "0.03", "10", "0.05")
        executor = self._make_executor_info(
            "EX-BUY", TradeType.BUY, price="315", level_id="buy_2", custom_info=info
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        # owned_quote -= (filled_quote + fees); owned_base += filled_base
        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("89.95"))
        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("0.53"))
        self.assertIn("EX-BUY", controller._state["booked_fill_progress"])
        self.assertEqual(self._emit_count(controller, "range_ladder_ledger_updated"), 1)

    # ------------------------------------------------------------ idempotency

    def test_fill_is_booked_once_idempotent(self):
        controller = self._make_controller(owned_quote="100", owned_base="0.5")
        info = self._exact_fill_custom_info(TradeType.SELL, "0.08", "27.72", "0.05")
        executor = self._make_executor_info(
            "EX-SELL", TradeType.SELL, price="350", level_id="sell_0", custom_info=info
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()
        first_quote = self._d(controller._state["owned_quote"])
        first_base = self._d(controller._state["owned_base"])

        # Second pass over the same finished executor must NOT re-book.
        controller._update_ledger_from_completed_executors()

        self.assertEqual(self._d(controller._state["owned_quote"]), first_quote)
        self.assertEqual(self._d(controller._state["owned_base"]), first_base)
        # Booked once: the per-order progress entry reflects the single fill, not re-applied.
        self.assertIn("EX-SELL", controller._state["booked_fill_progress"])
        self.assertEqual(self._d(controller._state["booked_fill_progress"]["EX-SELL"]["base"]), Decimal("0.08"))
        # Event emitted exactly once across both calls.
        self.assertEqual(self._emit_count(controller, "range_ladder_ledger_updated"), 1)

    # ----------------------------------------------------- regression (the bug)

    def test_public_filled_zero_but_custom_info_populated_is_still_booked(self):
        """REGRESSION: the exact scenario from the 45-hour run — the public
        filled_amount_quote is 0 (OrderExecutor hardcodes it), yet the real fill
        lives in custom_info. It MUST be booked. Reverting Part B breaks this."""
        controller = self._make_controller(owned_quote="100", owned_base="0.5")
        info = self._exact_fill_custom_info(TradeType.SELL, "0.08", "27.72", "0.05")
        executor = self._make_executor_info(
            "EX-BUGGY",
            TradeType.SELL,
            price="350",
            level_id="sell_0",
            custom_info=info,
            public_filled_quote="0",   # <-- the always-zero public property
            public_fees="0",
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("127.67"))
        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("0.42"))
        self.assertIn("EX-BUGGY", controller._state["booked_fill_progress"])
        self.assertEqual(self._emit_count(controller, "range_ladder_ledger_updated"), 1)

    # --------------------------------------------------------- v13 source policy

    def test_public_only_fields_not_booked_in_v13(self):
        """v13 books from the in-flight order / custom_info, NOT the executor's public
        filled_amount_quote (OrderExecutor hardcodes that to 0). A fill signalled ONLY by the
        public field is therefore not booked -- v13 never relies on a field that is always 0."""
        controller = self._make_controller(owned_quote="50", owned_base="1.0")
        executor = self._make_executor_info(
            "EX-LEGACY",
            TradeType.SELL,
            price="320",
            level_id="sell_0",
            custom_info={},               # no custom_info fill fields
            public_filled_quote="320",    # only the (unreliable) public field
            public_fees="0.16",
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        # Unchanged: the public field is not a v13 booking source (no double-count risk, and no
        # phantom fund growth from OrderExecutor's hardcoded-0 public property).
        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("1.0"))
        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("50"))
        self.assertEqual(self._emit_count(controller, "range_ladder_ledger_updated"), 0)

    # ----------------------------------------------------------- not-ours guard

    def test_executor_without_level_id_is_ignored(self):
        controller = self._make_controller(owned_quote="100", owned_base="0.5")
        info = self._exact_fill_custom_info(TradeType.SELL, "0.08", "27.72", "0.05")
        executor = self._make_executor_info(
            "EX-FOREIGN", TradeType.SELL, price="350", level_id=None, custom_info=info
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        # Untouched: not our level.
        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("100"))
        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("0.5"))
        self.assertNotIn("EX-FOREIGN", controller._state["tracked_fill_executor_ids"])
        self.assertEqual(self._emit_count(controller, "range_ladder_ledger_updated"), 0)

    # ----------------------------------------------------- active partial booking

    def test_active_executor_partial_is_booked(self):
        """v13: partials are booked AS THEY ACCRUE, so an ACTIVE executor's reported fill IS
        booked. The v12 'skip while active' gate is gone -- that gate is exactly why partial
        fills were missed and the fund never compounded."""
        controller = self._make_controller(owned_quote="100", owned_base="0.5")
        info = self._exact_fill_custom_info(TradeType.SELL, "0.08", "27.72", "0.05")
        executor = self._make_executor_info(
            "EX-ACTIVE", TradeType.SELL, price="350", level_id="sell_0",
            custom_info=info, is_active=True,
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("127.67"))
        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("0.42"))
        self.assertIn("EX-ACTIVE", controller._state["booked_fill_progress"])
        self.assertEqual(self._emit_count(controller, "range_ladder_ledger_updated"), 1)

    # --------------------------------------------------------------- floor at 0

    def test_owned_base_floored_at_zero(self):
        controller = self._make_controller(owned_quote="100", owned_base="0.01")
        # SELL more base than we hold -> owned_base would go negative.
        info = self._exact_fill_custom_info(TradeType.SELL, "0.08", "27.72", "0.05")
        executor = self._make_executor_info(
            "EX-OVERSELL", TradeType.SELL, price="350", level_id="sell_0", custom_info=info
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("0"))
        # quote still credited
        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("127.67"))

    def test_owned_quote_floored_at_zero(self):
        controller = self._make_controller(owned_quote="5", owned_base="0.5")
        # BUY costs more quote than we hold -> owned_quote would go negative.
        info = self._exact_fill_custom_info(TradeType.BUY, "0.03", "10", "0.05")
        executor = self._make_executor_info(
            "EX-OVERBUY", TradeType.BUY, price="315", level_id="buy_2", custom_info=info
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        self.assertEqual(self._d(controller._state["owned_quote"]), Decimal("0"))
        self.assertEqual(self._d(controller._state["owned_base"]), Decimal("0.53"))

    # ----------------------------------------------------- persistence to file

    def test_booking_persists_to_state_file(self):
        controller = self._make_controller(owned_quote="100", owned_base="0.5")
        info = self._exact_fill_custom_info(TradeType.SELL, "0.08", "27.72", "0.05")
        executor = self._make_executor_info(
            "EX-SELL", TradeType.SELL, price="350", level_id="sell_0", custom_info=info
        )
        controller.executors_info = [executor]

        controller._update_ledger_from_completed_executors()

        self.assertTrue(self._state_path.exists())
        import json
        with self._state_path.open() as f:
            persisted = json.load(f)
        self.assertEqual(self._d(persisted["owned_quote"]), Decimal("127.67"))
        self.assertEqual(self._d(persisted["owned_base"]), Decimal("0.42"))
        self.assertIn("EX-SELL", persisted["booked_fill_progress"])


if __name__ == "__main__":
    unittest.main()
