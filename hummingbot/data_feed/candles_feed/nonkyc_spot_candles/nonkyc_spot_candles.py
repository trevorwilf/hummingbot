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

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, trading_pair: str, interval: str = "5m", max_records: int = 150):
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
        bars = data.get("bars", []) if isinstance(data, dict) else data
        if not bars:
            return []
        result = []
        for bar in bars:
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
        Parse NonKYC WS candle messages.

        Handles:
        - Subscription ack: {"jsonrpc": "2.0", "result": true, "id": 1} → ignored
        - snapshotCandles: array sorted newest-first → use first entry
        - updateCandles: usually single candle → use first entry
        """
        if data is None:
            return None
        method = data.get("method")
        if method is None and "result" in data:
            return None
        if method not in ("snapshotCandles", "updateCandles"):
            return None
        params = data.get("params", {})
        candles_data = params.get("data", [])
        if not candles_data:
            return None
        candle = candles_data[0]
        return self._parse_ws_candle(candle)

    def _parse_ws_candle(self, candle: dict) -> dict:
        """
        Convert a single NonKYC WS candle to the standard dict.

        WS fields use 'min'/'max' (not 'low'/'high') and ISO8601 timestamps.
        """
        ts_str = candle["timestamp"]
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        timestamp = dt.replace(tzinfo=timezone.utc).timestamp()
        return {
            "timestamp": timestamp,
            "open": candle["open"],
            "high": candle["max"],
            "low": candle["min"],
            "close": candle["close"],
            "volume": candle["volume"],
            "quote_asset_volume": 0.0,
            "n_trades": 0.0,
            "taker_buy_base_volume": 0.0,
            "taker_buy_quote_volume": 0.0,
        }
