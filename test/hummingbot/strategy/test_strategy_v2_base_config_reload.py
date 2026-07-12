"""Controller-config hot-reload fault-isolation tests (2026-07-11 production incident).

A hot-edit of ONE controller YAML briefly contained out-of-order sell prices
(9.7723,9.9326,10.1929,10.0532,10.272). Pydantic correctly rejected it, but the
ValidationError propagated load_controller_configs() -> update_controllers_configs() ->
on_tick(), producing 14 consecutive "Unexpected error running clock tick" ERRORs during
which ALL controllers stopped ticking until the file was fixed by hand.

Under test (strategy_v2_base fix):
 - per-file isolation: a failing config file is skipped; the remaining configs load.
 - last-known-good: the affected controller keeps running on its previous config.
 - startup: a file broken at startup is skipped with a warning; the others start.
 - warning dedup: ONE warning per distinct broken content, not one per tick.
 - recovery: fixing the file logs an INFO and the config applies again.
"""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.client import settings
from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.strategy import strategy_v2_base as sv2_module
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase

LOGGER_NAME = "hummingbot.strategy.strategy_v2_base"

BROKEN_SELLS = "9.7723,9.9326,10.1929,10.0532,10.272"   # 10.1929 before 10.0532
VALID_SELLS = "9.7723,9.9326,10.0532,10.1929,10.272"


def _yaml(controller_id: str, *, sells: str = "350,355", watchdog: int = 60) -> str:
    return (
        f"id: {controller_id}\n"
        "controller_name: range_inventory_ladder\n"
        "controller_type: market_making\n"
        "connector_name: nonkyc\n"
        "trading_pair: XMR-USDT\n"
        "total_amount_quote: 100\n"
        "buy_prices: 321,318\n"
        "buy_amounts_pct: 1,1\n"
        f"sell_prices: {sells}\n"
        "sell_amounts_pct: 1,1\n"
        "min_order_quote: 1\n"
        f"empty_side_watchdog_seconds: {watchdog}\n"
    )


def _yaml_b(*, sells: str = VALID_SELLS) -> str:
    return (
        "id: ctrl_b\n"
        "controller_name: range_inventory_ladder\n"
        "controller_type: market_making\n"
        "connector_name: nonkyc\n"
        "trading_pair: DASH-USDT\n"
        "total_amount_quote: 100\n"
        "buy_prices: 9.5,9.4\n"
        "buy_amounts_pct: 1,1\n"
        f"sell_prices: {sells}\n"
        "sell_amounts_pct: 1,1,1,1,1\n"
        "min_order_quote: 1\n"
    )


class _ReloadHarness(unittest.TestCase):

    def setUp(self):
        sv2_module._controller_config_load_failures.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conf_dir = Path(self._tmp.name)
        patcher = patch.object(settings, "CONTROLLERS_CONF_DIR_PATH", self.conf_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name: str, content: str):
        (self.conf_dir / name).write_text(content, encoding="utf-8")

    def _build_strategy(self, config_files):
        connector = MockPaperExchange()
        config = StrategyV2ConfigBase(
            script_file_name="test", controllers_config=config_files)
        with patch("asyncio.create_task", return_value=MagicMock()), \
                patch.object(StrategyV2Base, "listen_to_executor_actions",
                             return_value=AsyncMock()), \
                patch("hummingbot.strategy.strategy_v2_base.ExecutorOrchestrator"), \
                patch("hummingbot.strategy.strategy_v2_base.MarketDataProvider"), \
                patch("hummingbot.strategy.strategy_v2_base.MarketsRecorder") as recorder:
            recorder.get_instance.return_value = MagicMock()
            strategy = StrategyV2Base({"mock_paper_exchange": connector}, config=config)
        strategy.market_data_provider.ready = False  # config reload only; no trading path
        self._recorder_patch = patch(
            "hummingbot.strategy.strategy_v2_base.MarketsRecorder")
        recorder2 = self._recorder_patch.start()
        recorder2.get_instance.return_value = MagicMock()
        self.addCleanup(self._recorder_patch.stop)
        return strategy

    @staticmethod
    def _tick(strategy, t):
        strategy._set_current_timestamp(t)
        strategy.on_tick()


class TestHotReloadIsolation(_ReloadHarness):

    def test_corrupt_file_mid_run_does_not_freeze_the_strategy(self):
        """The mandated regression: two valid configs load; B is corrupted on disk with
        the exact production out-of-order sell_prices; 10 ticks raise nothing, controller
        A keeps receiving config updates, controller B retains its last-good config, and
        exactly 1 warning is emitted. Restoring B reloads it with an INFO."""
        self._write("a.yml", _yaml("ctrl_a"))
        self._write("b.yml", _yaml_b())
        strategy = self._build_strategy(["a.yml", "b.yml"])
        self.assertEqual({"ctrl_a", "ctrl_b"}, set(strategy.controllers.keys()))
        good_sells = list(strategy.controllers["ctrl_b"].config.sell_prices)

        # Corrupt B exactly as production observed it.
        self._write("b.yml", _yaml_b(sells=BROKEN_SELLS))

        with self.assertLogs(LOGGER_NAME, level="WARNING") as captured:
            t = 1000.0
            for i in range(10):
                self._tick(strategy, t)   # must not raise
                t += 11.0
        warnings = [r for r in captured.records
                    if r.levelno == logging.WARNING and "Skipping controller config" in r.getMessage()]
        self.assertEqual(1, len(warnings), [r.getMessage() for r in captured.records])
        self.assertIn("b.yml", warnings[0].getMessage())

        # B kept its last-known-good config the whole time.
        self.assertEqual(good_sells, list(strategy.controllers["ctrl_b"].config.sell_prices))

        # A continued to receive config updates while B was broken.
        self._write("a.yml", _yaml("ctrl_a", watchdog=77))
        self._tick(strategy, t)
        t += 11.0
        self.assertEqual(77, strategy.controllers["ctrl_a"].config.empty_side_watchdog_seconds)

        # Restore B: an INFO logs the recovery and the config applies again.
        self._write("b.yml", _yaml_b())
        with self.assertLogs(LOGGER_NAME, level="INFO") as captured:
            self._tick(strategy, t)
        infos = [r for r in captured.records if "reloaded successfully" in r.getMessage()]
        self.assertEqual(1, len(infos))
        self.assertIn("b.yml", infos[0].getMessage())
        self.assertEqual(good_sells, list(strategy.controllers["ctrl_b"].config.sell_prices))

    def test_rewarns_only_when_the_broken_content_changes(self):
        self._write("a.yml", _yaml("ctrl_a"))
        self._write("b.yml", _yaml_b())
        strategy = self._build_strategy(["a.yml", "b.yml"])

        self._write("b.yml", _yaml_b(sells=BROKEN_SELLS))
        with self.assertLogs(LOGGER_NAME, level="WARNING") as captured:
            for i in range(5):
                self._tick(strategy, 1000.0 + i * 11.0)
        first = [r for r in captured.records if "Skipping controller config" in r.getMessage()]
        self.assertEqual(1, len(first))

        # A DIFFERENT broken edit warns once more.
        self._write("b.yml", _yaml_b(sells="10.5,10.4,10.3,10.2,10.1"))
        with self.assertLogs(LOGGER_NAME, level="WARNING") as captured:
            for i in range(5):
                self._tick(strategy, 1100.0 + i * 11.0)
        second = [r for r in captured.records if "Skipping controller config" in r.getMessage()]
        self.assertEqual(1, len(second))

    def test_unreadable_file_is_isolated_too(self):
        self._write("a.yml", _yaml("ctrl_a"))
        self._write("b.yml", _yaml_b())
        strategy = self._build_strategy(["a.yml", "b.yml"])
        (self.conf_dir / "b.yml").unlink()
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            for i in range(3):
                self._tick(strategy, 1000.0 + i * 11.0)   # must not raise
        self.assertIn("ctrl_b", strategy.controllers)      # keeps running on last-good


class TestStartupIsolation(_ReloadHarness):

    def test_broken_file_at_startup_skips_that_controller_only(self):
        self._write("a.yml", _yaml("ctrl_a"))
        self._write("b.yml", _yaml_b(sells=BROKEN_SELLS))
        with self.assertLogs(LOGGER_NAME, level="WARNING") as captured:
            strategy = self._build_strategy(["a.yml", "b.yml"])
        self.assertEqual({"ctrl_a"}, set(strategy.controllers.keys()))
        warnings = [r for r in captured.records if "Skipping controller config" in r.getMessage()]
        self.assertEqual(1, len(warnings))

        # Fixing the file brings the controller up through the hot-update path.
        self._write("b.yml", _yaml_b())
        with self.assertLogs(LOGGER_NAME, level="INFO") as captured:
            self._tick(strategy, 1000.0)
        self.assertIn("ctrl_b", strategy.controllers)
        infos = [r for r in captured.records if "reloaded successfully" in r.getMessage()]
        self.assertEqual(1, len(infos))


if __name__ == "__main__":
    unittest.main()
