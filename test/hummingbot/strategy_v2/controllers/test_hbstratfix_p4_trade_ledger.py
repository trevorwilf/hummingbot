"""
hbstrat_fix Phase 4 tests — persisted trade-cap ledger (CDX-011 / CLA-305).

Covers:
- TradeLedger records only actually-FILLED executors (the executors_info buffer
  over-included never-filled executors); recording is idempotent per executor id.
- The ledger is persisted with atomic writes and survives a simulated restart
  (a fresh TradeLedger for the same controller id reloads the fills from disk).
- FAIL-SAFE IO: read/write failures degrade to in-memory accumulation with a
  WARNING and never raise into the caller.
- mean_reversion_bb_rsi_v1: max_trades_per_day counts the ledger, not the
  transient bot-wide executors_info buffer — the cap survives buffer eviction
  (co-deployed churn) and bot restarts; never-filled executors no longer count;
  the same-side cooldown takes the max of the buffer reference and the ledger.
- pmm_mister: the per-level cooldown keeps holding after the level's executors
  are evicted from the buffer or the bot restarts (ledger floor + ledger-known
  level union in analyze_all_levels), and releases once the cooldown elapses.

Expected values in these tests are derived from the finding/spec
(HBSTRAT_FINDINGS.md CDX-011/CLA-305 and the Phase 4 fix contract), not from
running the implementation.
"""
import asyncio
import json
import logging
import unittest
from decimal import Decimal
from pathlib import Path
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from controllers._shared.trade_ledger import TradeLedger
from controllers.directional_trading.ema_regime_hold_v1 import EMARegimeHoldV1, EMARegimeHoldV1Config
from controllers.directional_trading.mean_reversion_bb_rsi_v1 import MeanReversionBBRSIV1, MeanReversionBBRSIV1Config
from controllers.generic.pmm_mister import PMMister, PMMisterConfig
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import DirectionalTradingControllerBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class _StubExecutor:
    """Minimal duck-typed executor snapshot for TradeLedger unit tests."""

    def __init__(self, executor_id, filled_amount_quote=Decimal("0"), custom_info=None, config=None):
        self.id = executor_id
        self.filled_amount_quote = filled_amount_quote
        self.custom_info = custom_info if custom_info is not None else {}
        self.config = config


def make_executor_info(executor_id: str, side: TradeType, level_id=None,
                       filled_amount_quote: Decimal = Decimal("0"),
                       timestamp: float = 1000.0,
                       close_timestamp=None,
                       status: RunnableStatus = RunnableStatus.RUNNING,
                       is_trading: bool = False) -> ExecutorInfo:
    """Build a real ExecutorInfo the wired controllers can consume."""
    config = OrderExecutorConfig(
        id=executor_id,
        timestamp=timestamp,
        connector_name="nonkyc",
        trading_pair="XMR-USDT",
        side=side,
        amount=Decimal("1"),
        execution_strategy=ExecutionStrategy.LIMIT,
        price=Decimal("100"),
        level_id=level_id,
    )
    custom_info = {"side": side}
    if level_id is not None:
        custom_info["level_id"] = level_id
    is_active = status in (RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED)
    return ExecutorInfo(
        id=executor_id,
        timestamp=timestamp,
        type="order_executor",
        status=status,
        config=config,
        net_pnl_pct=Decimal("0"),
        net_pnl_quote=Decimal("0"),
        cum_fees_quote=Decimal("0"),
        filled_amount_quote=filled_amount_quote,
        is_active=is_active,
        is_trading=is_trading,
        custom_info=custom_info,
        close_timestamp=close_timestamp,
    )


class TestTradeLedgerUnit(unittest.TestCase):
    """Direct TradeLedger behavior: fills-only, idempotence, persistence, fail-safe IO."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)
        self.logger = logging.getLogger("test_trade_ledger_p4")

    def _ledger(self, ledger_id="ctl-1", **kwargs):
        kwargs.setdefault("base_dir", self.base_dir)
        kwargs.setdefault("logger", self.logger)
        return TradeLedger(ledger_id=ledger_id, **kwargs)

    def test_only_filled_executors_are_recorded(self):
        ledger = self._ledger()
        filled = _StubExecutor("e-filled", filled_amount_quote=Decimal("25"),
                               custom_info={"side": TradeType.BUY})
        unfilled = _StubExecutor("e-unfilled", filled_amount_quote=Decimal("0"),
                                 custom_info={"side": TradeType.BUY})
        new = ledger.observe_executors([filled, unfilled], now=1000.0)
        self.assertEqual(1, new)
        self.assertEqual(1, ledger.count_fills_since(0.0))
        self.assertEqual(1000.0, ledger.last_fill_timestamp())

    def test_observation_is_idempotent_per_executor(self):
        ledger = self._ledger()
        filled = _StubExecutor("e-1", filled_amount_quote=Decimal("25"),
                               custom_info={"side": TradeType.BUY})
        ledger.observe_executors([filled], now=1000.0)
        ledger.observe_executors([filled], now=2000.0)
        self.assertEqual(1, ledger.count_fills_since(0.0))
        # Unchanged fill amount must NOT re-stamp the trade at the later time.
        self.assertEqual(1000.0, ledger.last_fill_timestamp())

    def test_partial_fill_growth_restamps_but_counts_once(self):
        ledger = self._ledger()
        executor = _StubExecutor("e-1", filled_amount_quote=Decimal("10"),
                                 custom_info={"side": TradeType.BUY})
        ledger.observe_executors([executor], now=1000.0)
        executor.filled_amount_quote = Decimal("20")
        ledger.observe_executors([executor], now=2000.0)
        self.assertEqual(1, ledger.count_fills_since(0.0))
        self.assertEqual(2000.0, ledger.last_fill_timestamp())

    def test_custom_info_fill_detection(self):
        # OrderExecutor POSITION_HOLD keeps the public filled_amount_quote at 0
        # and reports fills via custom_info; the ledger must see those too.
        ledger = self._ledger()
        executor = _StubExecutor("e-hold", filled_amount_quote=Decimal("0"),
                                 custom_info={"side": TradeType.SELL,
                                              "level_id": "sell_0",
                                              "filled_amount_quote": Decimal("30")})
        ledger.observe_executors([executor], now=1500.0)
        self.assertEqual(1, ledger.count_fills_since(0.0))
        self.assertEqual(1500.0, ledger.last_fill_timestamp(level_id="sell_0"))

    def test_persistence_survives_restart(self):
        ledger = self._ledger(ledger_id="restart-me")
        executor = _StubExecutor("e-1", filled_amount_quote=Decimal("25"),
                                 custom_info={"side": TradeType.BUY, "level_id": "buy_0"})
        ledger.observe_executors([executor], now=5000.0)

        reborn = self._ledger(ledger_id="restart-me")
        self.assertEqual(1, reborn.count_fills_since(0.0))
        self.assertEqual(5000.0, reborn.last_fill_timestamp(side=TradeType.BUY))
        self.assertEqual(5000.0, reborn.last_fill_timestamp(level_id="buy_0"))
        self.assertEqual(["buy_0"], reborn.level_ids_with_fills_since(0.0))

    def test_atomic_write_publishes_via_fsync_then_replace(self):
        # CDX-R03: a plain overwrite also leaves valid JSON and no .tmp file, so
        # asserting only the final state cannot catch a non-atomic regression.
        # Assert the WRITE PROTOCOL: the payload is fsync'd and then published
        # with a single os.replace of the tmp file onto the ledger path.
        from controllers._shared import trade_ledger as tl_module
        protocol_calls = []
        real_fsync = tl_module.os.fsync
        real_replace = tl_module.os.replace

        def spy_fsync(fd):
            protocol_calls.append(("fsync",))
            return real_fsync(fd)

        def spy_replace(src, dst):
            protocol_calls.append(("replace", str(src), str(dst)))
            return real_replace(src, dst)

        ledger = self._ledger(ledger_id="atomic")
        with patch.object(tl_module.os, "fsync", side_effect=spy_fsync), \
                patch.object(tl_module.os, "replace", side_effect=spy_replace):
            ledger.observe_executors(
                [_StubExecutor("e-1", filled_amount_quote=Decimal("1"))], now=1.0)

        replace_calls = [c for c in protocol_calls if c[0] == "replace"]
        self.assertEqual(1, len(replace_calls),
                         f"expected exactly one atomic publish, got {protocol_calls}")
        _, src, dst = replace_calls[0]
        self.assertTrue(src.endswith(".tmp"), f"replace source must be the tmp file: {src}")
        self.assertEqual(str(ledger.path), dst)
        # The file fsync must happen BEFORE the publish (torn-file protection).
        replace_index = protocol_calls.index(replace_calls[0])
        self.assertIn(("fsync",), protocol_calls[:replace_index],
                      f"no fsync before the publish: {protocol_calls}")
        # Final state still holds: no leftover tmp files, valid published JSON.
        leftovers = list(self.base_dir.glob("*.tmp"))
        self.assertEqual([], leftovers)
        payload = json.loads(ledger.path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("atomic", payload["ledger_id"])
        self.assertEqual(1, len(payload["records"]))
        self.assertEqual("e-1", payload["records"][0]["executor_id"])

    def test_write_failure_degrades_to_in_memory_with_warning(self):
        # Using an existing FILE as the base directory makes mkdir/persist fail.
        blocker = self.base_dir / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        ledger = TradeLedger(ledger_id="io-fail", base_dir=blocker / "sub", logger=self.logger)
        with self.assertLogs(self.logger, level="WARNING") as logs:
            new = ledger.observe_executors(
                [_StubExecutor("e-1", filled_amount_quote=Decimal("9"))], now=100.0)
        self.assertEqual(1, new)
        # In-memory behavior is intact despite the failed write.
        self.assertEqual(1, ledger.count_fills_since(0.0))
        self.assertEqual(100.0, ledger.last_fill_timestamp())
        self.assertTrue(any("persist" in message for message in logs.output),
                        f"expected a persist warning, got {logs.output}")
        self.assertGreaterEqual(ledger.io_failure_count, 1)

    def test_corrupt_file_degrades_to_empty_with_warning_and_quarantine(self):
        path = self._ledger(ledger_id="corrupt-1").path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        with self.assertLogs(self.logger, level="WARNING"):
            ledger = TradeLedger(ledger_id="corrupt-1", base_dir=self.base_dir, logger=self.logger)
        self.assertEqual(0, ledger.count_fills_since(0.0))
        self.assertFalse(path.exists())
        self.assertTrue(Path(f"{path}.corrupt").exists())

    def test_foreign_ledger_id_is_not_adopted_and_not_quarantined(self):
        # A copied/restored file carrying another controller's ledger_id under
        # our exact file name must not be adopted (and must not be destroyed).
        path = self._ledger(ledger_id="mine").path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": 1,
            "ledger_id": "somebody-else",
            "records": [{"executor_id": "e-x", "timestamp": 10.0,
                         "side": "BUY", "level_id": None, "amount_quote": 5.0}],
        }), encoding="utf-8")
        with self.assertLogs(self.logger, level="WARNING"):
            ledger = TradeLedger(ledger_id="mine", base_dir=self.base_dir, logger=self.logger)
        self.assertEqual(0, ledger.count_fills_since(0.0))
        # The foreign file is left in place (it may be someone's live ledger).
        self.assertTrue(path.exists())

    def test_distinct_ids_with_colliding_sanitizations_do_not_share_history(self):
        # CDX-R02: 'ctl/a' and 'ctl:a' both sanitize to 'ctl_a'. With a lossy
        # sanitized-only file name they alias to ONE path: the second ledger
        # overwrites the first controller's file, and the reborn first
        # controller then sees a foreign ledger_id and silently restarts with
        # zero cap/cooldown history. Distinct ids must never share a file.
        first = self._ledger(ledger_id="ctl/a")
        first.observe_executors(
            [_StubExecutor("e-a", filled_amount_quote=Decimal("5"),
                           custom_info={"side": TradeType.BUY})], now=1000.0)
        second = self._ledger(ledger_id="ctl:a")
        second.observe_executors(
            [_StubExecutor("e-b", filled_amount_quote=Decimal("7"),
                           custom_info={"side": TradeType.SELL})], now=2000.0)
        self.assertNotEqual(first.path, second.path)

        # Simulated restart of the first controller: its history must survive
        # the second controller's writes.
        reborn = self._ledger(ledger_id="ctl/a")
        self.assertEqual(1, reborn.count_fills_since(0.0))
        self.assertEqual(1000.0, reborn.last_fill_timestamp())
        # And the second controller's own restart sees its own record.
        reborn_second = self._ledger(ledger_id="ctl:a")
        self.assertEqual(2000.0, reborn_second.last_fill_timestamp())

    def test_nan_and_garbage_amounts_are_not_recorded(self):
        ledger = self._ledger()
        executors = [
            _StubExecutor("e-nan-dec", filled_amount_quote=Decimal("NaN")),
            _StubExecutor("e-nan-float", filled_amount_quote=float("nan")),
            _StubExecutor("e-inf", filled_amount_quote=Decimal("Infinity")),
            _StubExecutor("e-none", filled_amount_quote=None),
            _StubExecutor("e-str", filled_amount_quote="garbage"),
            _StubExecutor("e-neg", filled_amount_quote=Decimal("-5")),
        ]
        new = ledger.observe_executors(executors, now=1000.0)
        self.assertEqual(0, new)
        self.assertEqual(0, ledger.count_fills_since(0.0))

    def test_retention_prunes_old_records(self):
        ledger = self._ledger(ledger_id="prune", retention_seconds=100.0)
        ledger.observe_executors(
            [_StubExecutor("e-old", filled_amount_quote=Decimal("1"))], now=1000.0)
        ledger.observe_executors(
            [_StubExecutor("e-new", filled_amount_quote=Decimal("1"))], now=1200.0)
        # e-old (age 200 > retention 100) must be gone; e-new must remain.
        self.assertEqual(1, ledger.count_fills_since(0.0))
        self.assertEqual(1200.0, ledger.last_fill_timestamp())

    def test_side_and_level_filters(self):
        ledger = self._ledger()
        ledger.observe_executors(
            [_StubExecutor("e-b", filled_amount_quote=Decimal("1"),
                           custom_info={"side": TradeType.BUY, "level_id": "buy_1"})], now=1000.0)
        ledger.observe_executors(
            [_StubExecutor("e-s", filled_amount_quote=Decimal("1"),
                           custom_info={"side": TradeType.SELL, "level_id": "sell_2"})], now=2000.0)
        self.assertEqual(1, ledger.count_fills_since(0.0, side=TradeType.BUY))
        self.assertEqual(1, ledger.count_fills_since(0.0, side=TradeType.SELL))
        self.assertEqual(2, ledger.count_fills_since(0.0))
        self.assertEqual(1000.0, ledger.last_fill_timestamp(side=TradeType.BUY))
        self.assertEqual(2000.0, ledger.last_fill_timestamp(side="sell"))
        self.assertEqual(["buy_1", "sell_2"], ledger.level_ids_with_fills_since(0.0))
        self.assertEqual(["sell_2"], ledger.level_ids_with_fills_since(1500.0))

    def test_default_dir_and_file_name_sanitization(self):
        # No base_dir: composes under the module default (patched per-test by
        # conftest; Path("data") in production). File name = readable sanitized
        # fragment + digest of the FULL id (CDX-R02 collision resistance).
        import hashlib
        from controllers._shared import trade_ledger as tl_module
        ledger = TradeLedger(ledger_id="a/b:c d", logger=self.logger)
        self.assertEqual(tl_module.DEFAULT_LEDGER_DIR, ledger.path.parent)
        expected_digest = hashlib.sha256("a/b:c d".encode("utf-8")).hexdigest()[:12]
        self.assertEqual(f"trade_ledger_a_b_c_d_{expected_digest}.json", ledger.path.name)


class TestMeanReversionTradeCapLedger(IsolatedAsyncioWrapperTestCase):
    """CDX-011 / CLA-305 wiring in mean_reversion_bb_rsi_v1."""

    NOW = 100000.0

    def _make_config(self, **overrides):
        kwargs = dict(
            id="mr-ledger-test",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            candles_connector="nonkyc",
            candles_trading_pair="XMR-USDT",
            cooldown_time=3600,
            max_trades_per_day=6,
            max_spread_pct=0.0,  # disable the spread gate; not under test here
        )
        kwargs.update(overrides)
        return MeanReversionBBRSIV1Config(**kwargs)

    def _make_controller(self, config=None):
        config = config or self._make_config()
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=self.NOW)
        controller = MeanReversionBBRSIV1(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def _seed_fills(self, controller, count, fill_time, side=TradeType.BUY):
        fills = [
            make_executor_info(f"fill-{side.name}-{i}", side,
                               filled_amount_quote=Decimal("25"),
                               timestamp=fill_time, close_timestamp=fill_time,
                               status=RunnableStatus.TERMINATED)
            for i in range(count)
        ]
        controller._trade_ledger.observe_executors(fills, now=fill_time)

    def _can_create(self, controller, signal=1):
        with patch.object(DirectionalTradingControllerBase, "can_create_executor", return_value=True):
            return controller.can_create_executor(signal)

    def test_daily_cap_counts_ledger_not_transient_buffer(self):
        controller = self._make_controller()
        # 6 fills, 2h old: inside the 24h cap window, outside the 1h cooldown.
        self._seed_fills(controller, 6, fill_time=self.NOW - 7200)
        # The bot-wide buffer has since evicted every one of them.
        controller.executors_info = []
        self.assertFalse(self._can_create(controller))

    def test_cap_not_reset_by_co_deployed_churn(self):
        controller = self._make_controller()
        self._seed_fills(controller, 6, fill_time=self.NOW - 7200)
        # Churn replaced the buffer contents with newer, never-filled executors
        # (fewer than the cap, on the opposite side so the cooldown gate is not
        # what blocks): the old count would see 3 < 6 and fail open.
        controller.executors_info = [
            make_executor_info(f"churn-{i}", TradeType.SELL,
                               filled_amount_quote=Decimal("0"),
                               timestamp=self.NOW - 60, close_timestamp=self.NOW - 60,
                               status=RunnableStatus.TERMINATED)
            for i in range(3)
        ]
        self.assertFalse(self._can_create(controller))

    def test_never_filled_executors_do_not_count_toward_cap(self):
        controller = self._make_controller()
        # 10 never-filled same-side executors, 2h old: within the 24h window the
        # old buffer count saw 10 >= 6 and blocked; only fills may count now.
        controller.executors_info = [
            make_executor_info(f"unfilled-{i}", TradeType.BUY,
                               filled_amount_quote=Decimal("0"),
                               timestamp=self.NOW - 7200, close_timestamp=self.NOW - 7200,
                               status=RunnableStatus.TERMINATED)
            for i in range(10)
        ]
        self.assertTrue(self._can_create(controller))

    def test_cap_survives_restart(self):
        config = self._make_config(id="mr-restart-test")
        first_life = self._make_controller(config)
        self._seed_fills(first_life, 6, fill_time=self.NOW - 7200)

        # Restart: fresh controller instance, empty executors_info buffer. Both
        # controllers resolve the same default ledger path for the same id.
        second_life = self._make_controller(config)
        self.assertEqual(6, second_life._trade_ledger.count_fills_since(0.0))
        self.assertFalse(self._can_create(second_life))

    def test_cooldown_survives_restart_and_releases_after_expiry(self):
        config = self._make_config(id="mr-cooldown-test", max_trades_per_day=0)
        first_life = self._make_controller(config)
        fill_time = self.NOW - 1800  # half the 3600s cooldown ago
        self._seed_fills(first_life, 1, fill_time=fill_time)

        second_life = self._make_controller(config)
        # Empty buffer after restart: only the ledger can know about the fill.
        self.assertFalse(self._can_create(second_life))

        # Not over-restrictive: once the cooldown elapses the gate opens.
        second_life.market_data_provider.time = MagicMock(return_value=fill_time + 3601.0)
        self.assertTrue(self._can_create(second_life))

    def test_opposite_side_fill_does_not_block_cooldown(self):
        config = self._make_config(id="mr-sides-test", max_trades_per_day=0)
        controller = self._make_controller(config)
        self._seed_fills(controller, 1, fill_time=self.NOW - 60, side=TradeType.SELL)
        controller.executors_info = []
        # A recent SELL fill must not cool down BUY entries.
        self.assertTrue(self._can_create(controller, signal=1))
        # ...but it does cool down SELL entries.
        self.assertFalse(self._can_create(controller, signal=-1))

    def test_io_failure_degrades_to_in_memory_and_does_not_raise(self):
        controller = self._make_controller()
        blocker_parent = controller._trade_ledger.path.parent
        blocker_parent.mkdir(parents=True, exist_ok=True)
        blocker = blocker_parent / "blocking_file"
        blocker.write_text("x", encoding="utf-8")
        test_logger = logging.getLogger("test_mr_ledger_io_fail")
        controller._trade_ledger = TradeLedger(
            ledger_id="mr-io-fail", base_dir=blocker / "sub", logger=test_logger)
        with self.assertLogs(test_logger, level="WARNING"):
            self._seed_fills(controller, 6, fill_time=self.NOW - 7200)
        controller.executors_info = []
        # The cap still holds from in-memory records, and nothing raised.
        self.assertFalse(self._can_create(controller))

    async def test_update_processed_data_observes_fills(self):
        controller = self._make_controller()
        controller.executors_info = [
            make_executor_info("live-fill", TradeType.BUY,
                               filled_amount_quote=Decimal("25"),
                               timestamp=self.NOW - 10)
        ]
        controller.market_data_provider.get_candles_df = MagicMock(return_value=None)
        await controller.update_processed_data()
        self.assertEqual(1, controller._trade_ledger.count_fills_since(0.0))
        self.assertEqual(self.NOW, controller._trade_ledger.last_fill_timestamp())


class TestEMARegimeHoldCooldownLedger(IsolatedAsyncioWrapperTestCase):
    """CDX-011 / CLA-305 wiring in ema_regime_hold_v1 (review CDX-R01).

    The EMA same-side cooldown was derived exclusively from the transient
    bot-wide executors_info buffer: a restart (or co-deployed churn eviction)
    reset last_ts to 0.0 and immediately permitted a same-side re-entry while
    the cooldown should still be active.
    """

    NOW = 100000.0

    def _make_config(self, **overrides):
        kwargs = dict(
            id="ema-ledger-test",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            candles_connector="nonkyc",
            candles_trading_pair="XMR-USDT",
            cooldown_time=3600,
        )
        kwargs.update(overrides)
        return EMARegimeHoldV1Config(**kwargs)

    def _make_controller(self, config=None):
        config = config or self._make_config()
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=self.NOW)
        return EMARegimeHoldV1(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )

    def _seed_fill(self, controller, fill_time, side=TradeType.BUY):
        executor = make_executor_info(f"ema-fill-{side.name}", side,
                                      filled_amount_quote=Decimal("25"),
                                      timestamp=fill_time, close_timestamp=fill_time,
                                      status=RunnableStatus.TERMINATED)
        controller._trade_ledger.observe_executors([executor], now=fill_time)

    def _can_create(self, controller, signal=1):
        with patch.object(DirectionalTradingControllerBase, "can_create_executor", return_value=True):
            return controller.can_create_executor(signal)

    def test_cooldown_survives_buffer_eviction(self):
        controller = self._make_controller()
        # Fill 60s ago, cooldown 3600s — but the buffer has evicted it.
        self._seed_fill(controller, fill_time=self.NOW - 60.0)
        controller.executors_info = []
        self.assertFalse(self._can_create(controller))

    def test_cooldown_survives_restart_and_releases_after_expiry(self):
        config = self._make_config(id="ema-restart-test")
        first_life = self._make_controller(config)
        fill_time = self.NOW - 60.0
        self._seed_fill(first_life, fill_time=fill_time)

        # Restart: fresh controller instance, empty executors_info buffer.
        # Only the persisted ledger can know about the pre-restart fill.
        second_life = self._make_controller(config)
        second_life.executors_info = []
        self.assertFalse(self._can_create(second_life))

        # Not over-restrictive: once the cooldown elapses the gate opens.
        second_life.market_data_provider.time = MagicMock(return_value=fill_time + 3601.0)
        self.assertTrue(self._can_create(second_life))

    async def test_update_processed_data_observes_fills(self):
        controller = self._make_controller()
        controller.executors_info = [
            make_executor_info("ema-live-fill", TradeType.BUY,
                               filled_amount_quote=Decimal("25"),
                               timestamp=self.NOW - 10)
        ]
        # Candle outage: update_processed_data early-returns, but the fill must
        # still have been observed into the ledger first.
        controller.market_data_provider.get_candles_df = MagicMock(return_value=None)
        await controller.update_processed_data()
        self.assertEqual(1, controller._trade_ledger.count_fills_since(0.0))
        self.assertEqual(self.NOW, controller._trade_ledger.last_fill_timestamp())


class TestPMMisterCooldownLedger(IsolatedAsyncioWrapperTestCase):
    """CDX-011 / CLA-305 wiring in pmm_mister: per-level cooldown durability."""

    def _make_config(self, **overrides):
        kwargs = dict(
            id="pmm-mister-ledger-test",
            connector_name="nonkyc",
            trading_pair="XMR-USDT",
            total_amount_quote=Decimal("1000"),
            # pass explicitly — pydantic v2 does not run "before" validators on
            # defaults, so string defaults would reach the controller unparsed
            buy_spreads="0.0005",
            sell_spreads="0.0005",
            buy_amounts_pct="1",
            sell_amounts_pct="1",
            buy_cooldown_time=60,
            sell_cooldown_time=60,
        )
        kwargs.update(overrides)
        return PMMisterConfig(**kwargs)

    def _make_controller(self, config=None, now=1000.0):
        config = config or self._make_config()
        market_data_provider = MagicMock(spec=MarketDataProvider)
        market_data_provider.time = MagicMock(return_value=now)
        market_data_provider.quantize_order_amount = MagicMock(
            side_effect=lambda connector, pair, amount: amount)
        controller = PMMister(
            config=config,
            market_data_provider=market_data_provider,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def _record_level_fill(self, controller, level_id, side, fill_time):
        executor = make_executor_info(f"fill-{level_id}", side, level_id=level_id,
                                      filled_amount_quote=Decimal("25"),
                                      timestamp=fill_time, close_timestamp=fill_time,
                                      status=RunnableStatus.TERMINATED)
        controller._trade_ledger.observe_executors([executor], now=fill_time)

    def _set_processed_data(self, controller):
        controller.processed_data = {
            "reference_price": Decimal("100"),
            "current_base_pct": Decimal("0.5"),
        }

    def test_level_cooldown_survives_buffer_eviction(self):
        controller = self._make_controller(now=1030.0)
        self._record_level_fill(controller, "buy_0", TradeType.BUY, fill_time=1000.0)
        controller.executors_info = []  # evicted by co-deployed churn
        self._set_processed_data(controller)

        levels = controller.get_levels_to_execute()
        # 30s since the fill < 60s cooldown: buy_0 must still be held back even
        # though no executor for it survives in the buffer.
        self.assertNotIn("buy_0", levels)
        self.assertIn("sell_0", levels)

    def test_level_cooldown_releases_after_expiry(self):
        controller = self._make_controller(now=1061.0)
        self._record_level_fill(controller, "buy_0", TradeType.BUY, fill_time=1000.0)
        controller.executors_info = []
        self._set_processed_data(controller)

        levels = controller.get_levels_to_execute()
        # 61s since the fill > 60s cooldown: the gate must open again.
        self.assertIn("buy_0", levels)
        self.assertIn("sell_0", levels)

    def test_level_cooldown_survives_restart(self):
        config = self._make_config(id="pmm-mister-restart-test")
        first_life = self._make_controller(config, now=1000.0)
        self._record_level_fill(first_life, "buy_0", TradeType.BUY, fill_time=1000.0)

        second_life = self._make_controller(config, now=1030.0)
        self._set_processed_data(second_life)
        analysis = second_life._analyze_by_level_id("buy_0")
        self.assertEqual(1000.0, analysis["open_order_last_update"])
        self.assertNotIn("buy_0", second_life.get_levels_to_execute())

    def test_analyze_by_level_id_takes_max_of_buffer_and_ledger(self):
        controller = self._make_controller(now=1030.0)
        self._record_level_fill(controller, "buy_0", TradeType.BUY, fill_time=1000.0)
        # A buffered executor with a NEWER open_order_last_update must win...
        buffered = make_executor_info("buffered", TradeType.BUY, level_id="buy_0",
                                      timestamp=1010.0)
        buffered.custom_info["open_order_last_update"] = 1020.0
        controller.executors_info = [buffered]
        self.assertEqual(1020.0, controller._analyze_by_level_id("buy_0")["open_order_last_update"])
        # ...and an OLDER one must not drag the ledger floor down.
        buffered.custom_info["open_order_last_update"] = 900.0
        self.assertEqual(1000.0, controller._analyze_by_level_id("buy_0")["open_order_last_update"])

    def test_cooldown_status_display_survives_eviction(self):
        controller = self._make_controller(now=1030.0)
        self._record_level_fill(controller, "buy_0", TradeType.BUY, fill_time=1000.0)
        controller.executors_info = []
        status = controller._calculate_cooldown_status(1030.0)
        self.assertTrue(status["buy"]["active"])
        self.assertEqual(30.0, status["buy"]["remaining_time"])
        self.assertFalse(status["sell"]["active"])

    async def test_update_processed_data_observes_fills(self):
        controller = self._make_controller(now=2000.0)
        controller.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("100"))
        controller.executors_info = [
            make_executor_info("live-fill", TradeType.BUY, level_id="buy_0",
                               filled_amount_quote=Decimal("25"), timestamp=1990.0)
        ]
        await controller.update_processed_data()
        self.assertEqual(1, controller._trade_ledger.count_fills_since(0.0))
        self.assertEqual(2000.0, controller._trade_ledger.last_fill_timestamp(level_id="buy_0"))

    async def test_fills_recorded_even_when_price_unavailable(self):
        controller = self._make_controller(now=2000.0)
        controller.market_data_provider.get_price_by_type = MagicMock(return_value=None)
        controller.executors_info = [
            make_executor_info("outage-fill", TradeType.SELL, level_id="sell_0",
                               filled_amount_quote=Decimal("12"), timestamp=1990.0)
        ]
        await controller.update_processed_data()  # early-returns on no price
        self.assertEqual(1, controller._trade_ledger.count_fills_since(0.0))


if __name__ == "__main__":
    unittest.main()
