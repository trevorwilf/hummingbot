import logging
from datetime import datetime, timezone
from typing import List, Optional

from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.data_feed.candles_feed.candles_base import CandlesBase
from hummingbot.data_feed.candles_feed.nonkyc_spot_candles import constants as CONSTANTS
from hummingbot.logger import HummingbotLogger


class NonKYCSpotCandles(CandlesBase):
    """
    Candles data feed adapter for the NonKYC.io spot exchange.

    REST endpoint : GET https://api.nonkyc.io/api/v2/market/candles
    WebSocket     : wss://ws.nonkyc.io  (JSON-RPC 2.0, subscribeCandles)

    NonKYC does NOT provide quote_asset_volume, n_trades,
    taker_buy_base_volume, or taker_buy_quote_volume in its candle data.
    These fields are zero-filled in the output.
    """

    _logger: Optional[HummingbotLogger] = None

    # Auto-fallback for intervals NonKYC doesn't support (Dashboard defaults to 1m)
    _INTERVAL_FALLBACK = {"1m": "5m", "3m": "5m"}

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, trading_pair: str, interval: str = "1h", max_records: int = 150):
        # Auto-fallback for unsupported intervals before the base class bare-raises
        if interval in self._INTERVAL_FALLBACK:
            self.logger().warning(
                f"Interval '{interval}' not supported by NonKYC, "
                f"falling back to '{self._INTERVAL_FALLBACK[interval]}'"
            )
            interval = self._INTERVAL_FALLBACK[interval]
        if interval not in CONSTANTS.INTERVALS:
            raise ValueError(
                f"Interval '{interval}' is not supported by NonKYC. "
                f"Supported intervals: {list(CONSTANTS.INTERVALS.keys())}"
            )
        super().__init__(trading_pair, interval, max_records)

    @property
    def name(self):
        return f"nonkyc_{self._trading_pair}"

    @property
    def rest_url(self):
        return CONSTANTS.REST_URL

    @property
    def wss_url(self):
        return CONSTANTS.WSS_URL

    @property
    def health_check_url(self):
        return self.rest_url + CONSTANTS.HEALTH_CHECK_ENDPOINT

    @property
    def candles_url(self):
        return self.rest_url + CONSTANTS.CANDLES_ENDPOINT

    @property
    def candles_endpoint(self):
        return CONSTANTS.CANDLES_ENDPOINT

    @property
    def candles_max_result_per_rest_request(self):
        return CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST

    @property
    def rate_limits(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def intervals(self):
        return CONSTANTS.INTERVALS

    async def check_network(self) -> NetworkStatus:
        rest_assistant = await self._api_factory.get_rest_assistant()
        await rest_assistant.execute_request(
            url=self.health_check_url,
            throttler_limit_id=CONSTANTS.HEALTH_CHECK_ENDPOINT,
        )
        return NetworkStatus.CONNECTED

    def get_exchange_trading_pair(self, trading_pair: str) -> str:
        """Hummingbot uses 'BTC-USDT'; NonKYC uses 'BTC/USDT'."""
        return trading_pair.replace("-", "/")

    def _get_rest_candles_params(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: Optional[int] = CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST,
    ) -> dict:
        """
        Build query params for GET /market/candles.

        NonKYC params:
            symbol     — e.g. "BTC/USDT"
            resolution — interval in minutes (5, 15, 30, 60, …)
            from       — start unix timestamp in seconds
            to         — end unix timestamp in seconds
            countBack  — max number of bars to return
        """
        resolution = CONSTANTS.INTERVALS[self.interval]
        params = {
            "symbol": self._ex_trading_pair,
            "resolution": int(resolution),
            "countBack": limit or CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST,
        }
        if start_time:
            params["from"] = int(start_time)
        if end_time:
            params["to"] = int(end_time)
        return params

    def _parse_rest_candles(self, data: dict, end_time: Optional[int] = None) -> List[List[float]]:
        """
        Parse NonKYC REST candle response into the standard 10-column format.

        NonKYC response: {"bars": [{"time": N, "open": N, "high": N, "low": N, "close": N, "volume": N}]}

        Zero-fills: quote_asset_volume, n_trades, taker_buy_base_volume, taker_buy_quote_volume
        """
        if not isinstance(data, dict):
            self.logger().warning(f"Unexpected REST candle response type: {type(data)} — data: {data}")
            return []
        bars = data.get("bars", [])
        if not bars:
            if data.get("error") or data.get("message"):
                self.logger().warning(
                    f"NonKYC REST candle error: {data.get('error', data.get('message', 'unknown'))}"
                )
            return []
        result = []
        for bar in bars:
            try:
                timestamp = self.ensure_timestamp_in_seconds(bar["time"])
                result.append([
                    timestamp,
                    float(bar["open"]),
                    float(bar["high"]),
                    float(bar["low"]),
                    float(bar["close"]),
                    float(bar["volume"]),
                    0.0,   # quote_asset_volume  (not provided by NonKYC)
                    0.0,   # n_trades            (not provided by NonKYC)
                    0.0,   # taker_buy_base_volume  (not provided by NonKYC)
                    0.0,   # taker_buy_quote_volume (not provided by NonKYC)
                ])
            except (KeyError, ValueError, TypeError) as e:
                self.logger().warning(f"Failed to parse REST candle bar: {e} — data: {bar}")
        return result

    def ws_subscription_payload(self) -> dict:
        """
        JSON-RPC 2.0 subscription for NonKYC candle stream.
        The 'period' parameter is the interval in minutes (int).
        """
        period = int(CONSTANTS.INTERVALS[self.interval])
        return {
            "method": "subscribeCandles",
            "params": {
                "symbol": self._ex_trading_pair,
                "period": period,
            },
            "id": 1,
        }

    def _parse_websocket_message(self, data: dict):
        """
        Parse NonKYC WS candle messages into the standard dict expected by the base class.

        Handles three message types:
        - Subscription ack: {"result": true, "id": ...} → ignored (return None)
        - snapshotCandles: snapshot with array of candles → return latest candle
        - updateCandles: single candle update → return that candle

        The base class (_process_websocket_messages_task) expects a dict with
        standard candle keys, or None to skip.
        """
        if data is None:
            return None

        # Ignore subscription confirmations: {"jsonrpc": "2.0", "result": true, "id": 123}
        if "result" in data:
            return None

        method = data.get("method", "")

        # Only process candle methods
        if method not in ("snapshotCandles", "updateCandles"):
            return None

        params = data.get("params", {})
        candles_data = params.get("data", [])

        if not candles_data:
            return None

        # Both snapshot and update: parse the first (latest) candle.
        # For snapshots (newest-first), [0] is the latest candle.
        # The base class will use fill_historical_candles() via REST for older data.
        return self._parse_ws_candle(candles_data[0])

    def _parse_ws_candle(self, candle: dict):
        """
        Parse a single NonKYC WS candle object into the standard dict format.

        NonKYC WS candle fields:
        - timestamp: ISO8601 string (e.g., "2024-01-01T00:00:00.000Z")
        - open, close: string prices
        - min, max: string prices (NOT "low"/"high")
        - volume: string

        Returns dict with float values, or None on parse failure.
        """
        try:
            ts_str = candle["timestamp"]
            # Handle ISO8601 timestamp → epoch seconds
            if isinstance(ts_str, str):
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                timestamp = dt.timestamp()
            else:
                # Already numeric (milliseconds or seconds)
                timestamp = float(ts_str)
                if timestamp > 1e12:
                    timestamp = timestamp / 1000.0

            return {
                "timestamp": float(timestamp),
                "open": float(candle["open"]),
                "high": float(candle["max"]),      # NonKYC uses "max" not "high"
                "low": float(candle["min"]),        # NonKYC uses "min" not "low"
                "close": float(candle["close"]),
                "volume": float(candle["volume"]),
                "quote_asset_volume": 0.0,
                "n_trades": 0.0,
                "taker_buy_base_volume": 0.0,
                "taker_buy_quote_volume": 0.0,
            }
        except (KeyError, ValueError, TypeError) as e:
            self.logger().warning(f"Failed to parse WS candle: {e} — data: {candle}")
            return None
