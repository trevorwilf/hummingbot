import asyncio
import time
from decimal import Decimal
from typing import Dict, List, Optional, Set

from hummingbot.core.rate_oracle.sources.rate_source_base import RateSourceBase

# The pool tried in order until a source delivers prices. The exchanges an instance
# actually trades on are prepended at runtime via set_preferred_exchanges().
DEFAULT_FAILOVER_PRIORITY = ["gate_io", "coin_gecko", "kucoin", "coin_cap", "mexc"]

# CoinCap needs a symbol->asset-id map; mirror of CoinCapRateSourceMode's default.
DEFAULT_COIN_CAP_ASSETS_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDT": "tether",
    "CONV": "convergence",
    "FIRO": "zcoin",
    "BUSD": "binance-usd",
    "ONE": "harmony",
}


class FailoverRateSource(RateSourceBase):
    """A rate source backed by a POOL of rate sources with automatic failover.

    Behavior contract:
    - Sources are tried in preference order until one delivers prices; that source
      becomes ACTIVE and is used exclusively while healthy.
    - A blip on the active source is tolerated: only after ``failover_threshold``
      consecutive failed fetches (default 3) does the pool fail over -- and it then
      re-scans FROM THE TOP of the preference list, so a recovered preferred source
      is picked up automatically.
    - set_preferred_exchanges() prepends the exchanges this instance trades on
      (when a matching rate source exists), so a bot favors its own exchange's
      oracle before the shared pool.
    - When the whole pool is down, one exception is raised and re-scans are paused
      for ``rescan_cooldown_seconds`` so the sources are not hammered every tick.
    """

    def __init__(
        self,
        priority: Optional[List[str]] = None,
        sources: Optional[List[RateSourceBase]] = None,
        failover_threshold: int = 3,
        rescan_cooldown_seconds: float = 10.0,
    ):
        super().__init__()
        self._priority = list(priority) if priority is not None else list(DEFAULT_FAILOVER_PRIORITY)
        self._pool: List[RateSourceBase] = (
            list(sources) if sources is not None else self._build_pool(self._priority)
        )
        self._preferred: List[RateSourceBase] = []
        self._failover_threshold = max(1, int(failover_threshold))
        self._rescan_cooldown_seconds = float(rescan_cooldown_seconds)
        self._active_index: Optional[int] = None
        self._consecutive_failures = 0
        self._next_scan_ts = 0.0

    @property
    def name(self) -> str:
        sources = self._sources()
        if self._active_index is not None and self._active_index < len(sources):
            return f"failover({sources[self._active_index].name})"
        return "failover"

    @property
    def pool_names(self) -> List[str]:
        return [source.name for source in self._sources()]

    def set_preferred_exchanges(self, connector_names: List[str]):
        """Prepend the exchanges this instance trades on to the preference order (rung #1:
        'the exchange it is on'). Connectors without a registered rate source (e.g. kraken)
        are skipped with an INFO log. Resets the active source so the next fetch re-scans
        from the top with the new order."""
        preferred: List[RateSourceBase] = []
        seen: Set[str] = set()
        for connector_name in connector_names or []:
            name = str(connector_name).lower().replace("_paper_trade", "")
            if name in seen:
                continue
            seen.add(name)
            source = next((s for s in self._pool if s.name == name), None)
            if source is None:
                source = self._build_source_by_name(name)
            if source is None:
                self.logger().info(
                    f"No rate source exists for exchange '{name}' -- skipping the "
                    "own-exchange preference for it."
                )
                continue
            preferred.append(source)
        self._preferred = preferred
        self._active_index = None
        self._consecutive_failures = 0
        self._next_scan_ts = 0.0
        self.logger().info(f"Failover rate oracle priority: {self.pool_names}")

    async def get_prices(self, quote_token: Optional[str] = None) -> Dict[str, Decimal]:
        sources = self._sources()
        if not sources:
            raise Exception("The failover rate oracle pool is empty.")

        # Fast path: stick with the active source, tolerating blips below the threshold.
        if self._active_index is not None and self._active_index < len(sources):
            active = sources[self._active_index]
            prices = await self._try_source(active, quote_token)
            if prices:
                self._consecutive_failures = 0
                return prices
            self._consecutive_failures += 1
            if self._consecutive_failures < self._failover_threshold:
                self.logger().debug(
                    f"Rate source {active.name} fetch failed "
                    f"({self._consecutive_failures}/{self._failover_threshold})."
                )
                return {}
            self.logger().warning(
                f"Rate source {active.name} is down ({self._consecutive_failures} consecutive "
                "failures) -- scanning the failover pool from the top of the preference list."
            )
            self._active_index = None
            self._consecutive_failures = 0

        # Scan from the top until a source delivers.
        if time.monotonic() < self._next_scan_ts:
            return {}
        for index, source in enumerate(sources):
            prices = await self._try_source(source, quote_token)
            if prices:
                self._active_index = index
                self._consecutive_failures = 0
                self.logger().info(
                    f"Failover rate oracle now using {source.name} (priority #{index + 1})."
                )
                return prices
        self._next_scan_ts = time.monotonic() + self._rescan_cooldown_seconds
        raise Exception(
            f"All rate sources in the failover pool failed: {[s.name for s in sources]}. "
            f"Next re-scan in {self._rescan_cooldown_seconds:.0f}s."
        )

    def _sources(self) -> List[RateSourceBase]:
        preferred_names = {source.name for source in self._preferred}
        return self._preferred + [s for s in self._pool if s.name not in preferred_names]

    async def _try_source(self, source: RateSourceBase, quote_token: Optional[str]) -> Dict[str, Decimal]:
        try:
            prices = await source.get_prices(quote_token=quote_token)
            return prices or {}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().debug(f"Rate source {source.name} fetch raised: {e!r}")
            return {}

    @classmethod
    def _build_pool(cls, priority: List[str]) -> List[RateSourceBase]:
        pool: List[RateSourceBase] = []
        seen: Set[str] = set()
        for name in priority:
            normalized = str(name).strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            source = cls._build_source_by_name(normalized)
            if source is None:
                cls.logger().warning(
                    f"Unknown rate source '{normalized}' in the failover priority list -- skipping."
                )
                continue
            pool.append(source)
        return pool

    @staticmethod
    def _build_source_by_name(name: str) -> Optional[RateSourceBase]:
        # Imported lazily: rate_oracle imports this module to register the source.
        from hummingbot.core.rate_oracle.rate_oracle import RATE_ORACLE_SOURCES
        source_cls = RATE_ORACLE_SOURCES.get(name)
        if source_cls is None or source_cls is FailoverRateSource:
            return None
        if name == "coin_gecko":
            from hummingbot.data_feed.coin_gecko_data_feed.coin_gecko_constants import CoinGeckoAPITier
            return source_cls(extra_token_ids=[], api_key="", api_tier=CoinGeckoAPITier.PUBLIC)
        if name == "coin_cap":
            return source_cls(assets_map=dict(DEFAULT_COIN_CAP_ASSETS_MAP), api_key="")
        return source_cls()
