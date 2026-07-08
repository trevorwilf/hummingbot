"""FailoverRateSource: a pool of rate oracles with automatic failover.

Contract under test:
- Sources are tried in preference order until one delivers; that one stays ACTIVE.
- A blip is tolerated: only 3 consecutive failed fetches fail over, and the pool then
  re-scans FROM THE TOP of the list (so a recovered preferred source wins again).
- set_preferred_exchanges() prepends the exchanges the instance trades on when a matching
  rate source exists (nonkyc does, kraken does not -> skipped).
- Full-pool outage raises once, then re-scans are paused for the cooldown.
"""
import asyncio
import unittest
from decimal import Decimal
from typing import Dict, Optional

from hummingbot.client.config.client_config_map import RATE_SOURCE_MODES, ClientConfigMap, FailoverRateSourceMode
from hummingbot.core.rate_oracle.rate_oracle import RATE_ORACLE_SOURCES
from hummingbot.core.rate_oracle.sources.failover_rate_source import DEFAULT_FAILOVER_PRIORITY, FailoverRateSource
from hummingbot.core.rate_oracle.sources.rate_source_base import RateSourceBase


class _FakeSource(RateSourceBase):
    def __init__(self, name: str, price: Optional[str] = "100", down: bool = False):
        super().__init__()
        self._name = name
        self.price = price
        self.down = down
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def get_prices(self, quote_token: Optional[str] = None) -> Dict[str, Decimal]:
        self.calls += 1
        if self.down:
            raise IOError(f"{self._name} is down")
        if self.price is None:
            return {}
        return {"BTC-USDT": Decimal(self.price)}


class TestFailoverRateSource(unittest.TestCase):

    def _pool(self, *sources, threshold=3, cooldown=600.0):
        return FailoverRateSource(sources=list(sources), failover_threshold=threshold,
                                  rescan_cooldown_seconds=cooldown)

    @staticmethod
    def _fetch(source):
        return asyncio.run(source.get_prices(quote_token="USD"))

    # ------------------------------------------------------------ selection & stickiness

    def test_prefers_first_available_source(self):
        a, b = _FakeSource("a", down=True), _FakeSource("b", price="200")
        pool = self._pool(a, b)
        prices = self._fetch(pool)
        self.assertEqual(Decimal("200"), prices["BTC-USDT"])
        self.assertEqual("failover(b)", pool.name)

    def test_active_source_is_sticky_while_healthy(self):
        a, b = _FakeSource("a", price="100"), _FakeSource("b", price="200")
        pool = self._pool(a, b)
        for _ in range(5):
            prices = self._fetch(pool)
            self.assertEqual(Decimal("100"), prices["BTC-USDT"])
        self.assertEqual(0, b.calls)                     # never touched while a is healthy
        self.assertEqual("failover(a)", pool.name)

    def test_blip_below_threshold_keeps_active_source(self):
        a, b = _FakeSource("a", price="100"), _FakeSource("b", price="200")
        pool = self._pool(a, b)
        self._fetch(pool)                                # a becomes active
        a.down = True
        self.assertEqual({}, self._fetch(pool))          # failure 1: quiet, no failover
        self.assertEqual({}, self._fetch(pool))          # failure 2: quiet, no failover
        self.assertEqual(0, b.calls)
        a.down = False
        prices = self._fetch(pool)                       # blip over -> still on a
        self.assertEqual(Decimal("100"), prices["BTC-USDT"])
        self.assertEqual("failover(a)", pool.name)

    def test_fails_over_after_three_consecutive_failures(self):
        a, b = _FakeSource("a", price="100"), _FakeSource("b", price="200")
        pool = self._pool(a, b)
        self._fetch(pool)
        a.down = True
        self._fetch(pool)                                # 1
        self._fetch(pool)                                # 2
        prices = self._fetch(pool)                       # 3 -> re-scan -> b
        self.assertEqual(Decimal("200"), prices["BTC-USDT"])
        self.assertEqual("failover(b)", pool.name)

    def test_rescan_starts_from_top_and_recovers_preferred_source(self):
        a, b = _FakeSource("a", down=True), _FakeSource("b", price="200")
        pool = self._pool(a, b)
        self._fetch(pool)                                # active: b
        a.down = False                                   # the preferred source recovers
        b.down = True
        for _ in range(2):
            self.assertEqual({}, self._fetch(pool))      # b blips, tolerated
        prices = self._fetch(pool)                       # threshold -> scan from TOP -> a
        self.assertEqual(Decimal("100"), prices["BTC-USDT"])
        self.assertEqual("failover(a)", pool.name)

    def test_empty_prices_count_as_failure(self):
        a, b = _FakeSource("a", price=None), _FakeSource("b", price="200")
        pool = self._pool(a, b)
        prices = self._fetch(pool)
        self.assertEqual(Decimal("200"), prices["BTC-USDT"])

    # ------------------------------------------------------------ full-pool outage

    def test_all_down_raises_once_then_cooldown_suppresses_rescans(self):
        a, b = _FakeSource("a", down=True), _FakeSource("b", down=True)
        pool = self._pool(a, b, cooldown=600.0)
        with self.assertRaises(Exception):
            self._fetch(pool)
        calls_after_scan = (a.calls, b.calls)
        self.assertEqual({}, self._fetch(pool))          # inside cooldown: quiet, no calls
        self.assertEqual(calls_after_scan, (a.calls, b.calls))

    def test_zero_cooldown_rescans_immediately(self):
        a, b = _FakeSource("a", down=True), _FakeSource("b", down=True)
        pool = self._pool(a, b, cooldown=0.0)
        with self.assertRaises(Exception):
            self._fetch(pool)
        b.down = False                                   # recovery is picked up right away
        prices = self._fetch(pool)
        self.assertEqual(Decimal("100"), prices["BTC-USDT"])

    # ------------------------------------------------------------ own-exchange preference

    def test_set_preferred_exchanges_prepends_pool_member_without_duplicating(self):
        binance, nonkyc = _FakeSource("binance"), _FakeSource("nonkyc", price="300")
        pool = self._pool(binance, nonkyc)
        pool.set_preferred_exchanges(["nonkyc"])
        self.assertEqual(["nonkyc", "binance"], pool.pool_names)
        prices = self._fetch(pool)
        self.assertEqual(Decimal("300"), prices["BTC-USDT"])   # own exchange wins
        self.assertEqual(0, binance.calls)

    def test_set_preferred_exchanges_resets_active_source(self):
        binance, nonkyc = _FakeSource("binance"), _FakeSource("nonkyc", price="300")
        pool = self._pool(binance, nonkyc)
        self._fetch(pool)                                # active: binance
        pool.set_preferred_exchanges(["nonkyc"])
        prices = self._fetch(pool)                       # re-scan from new top
        self.assertEqual(Decimal("300"), prices["BTC-USDT"])

    def test_unknown_exchange_is_skipped(self):
        binance = _FakeSource("binance")
        pool = self._pool(binance)
        pool.set_preferred_exchanges(["kraken"])         # no kraken rate source exists
        self.assertEqual(["binance"], pool.pool_names)

    def test_paper_trade_suffix_is_normalized(self):
        binance, nonkyc = _FakeSource("binance"), _FakeSource("nonkyc")
        pool = self._pool(binance, nonkyc)
        pool.set_preferred_exchanges(["nonkyc_paper_trade"])
        self.assertEqual(["nonkyc", "binance"], pool.pool_names)

    def test_own_exchange_built_from_registry_when_not_in_pool(self):
        binance = _FakeSource("binance")
        pool = self._pool(binance)
        pool.set_preferred_exchanges(["nonkyc"])         # built from RATE_ORACLE_SOURCES
        self.assertEqual(["nonkyc", "binance"], pool.pool_names)

    # ------------------------------------------------------------ construction & config

    def test_default_pool_matches_requested_preference_order(self):
        pool = FailoverRateSource()
        self.assertEqual(DEFAULT_FAILOVER_PRIORITY, pool.pool_names)
        self.assertEqual(
            ["gate_io", "coin_gecko", "kucoin", "coin_cap", "mexc"],
            pool.pool_names,
        )

    def test_unknown_priority_entry_is_skipped(self):
        pool = FailoverRateSource(priority=["not_a_source", "binance"])
        self.assertEqual(["binance"], pool.pool_names)

    def test_registered_in_rate_oracle_sources(self):
        self.assertIs(FailoverRateSource, RATE_ORACLE_SOURCES["failover"])

    def test_config_mode_registered_and_builds_pool(self):
        self.assertIn("failover", RATE_SOURCE_MODES)
        mode = RATE_SOURCE_MODES["failover"]()
        source = mode.build_rate_source()
        self.assertIsInstance(source, FailoverRateSource)
        self.assertEqual(DEFAULT_FAILOVER_PRIORITY, source.pool_names)

    def test_config_mode_priority_accepts_comma_string(self):
        mode = FailoverRateSourceMode(priority="binance, kucoin")
        self.assertEqual(["binance", "kucoin"], mode.priority)
        self.assertEqual(["binance", "kucoin"], mode.build_rate_source().pool_names)

    def test_client_config_validator_accepts_failover_string(self):
        mode = ClientConfigMap.validate_rate_oracle_source("failover")
        self.assertIsInstance(mode, FailoverRateSourceMode)


if __name__ == "__main__":
    unittest.main()
