import asyncio
import json
import re
import time
from test.hummingbot.data_feed.candles_feed.test_candles_base import TestCandlesBase

from aioresponses import aioresponses

from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.data_feed.candles_feed.nonkyc_spot_candles import NonKYCSpotCandles, constants as CONSTANTS


class TestNonKYCSpotCandles(TestCandlesBase):
    __test__ = True
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.interval = "5m"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}/{cls.quote_asset}"
        cls.max_records = CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST

    def setUp(self) -> None:
        super().setUp()
        self.data_feed = NonKYCSpotCandles(trading_pair=self.trading_pair, interval=self.interval, max_records=self.max_records)

        self.log_records = []
        self.data_feed.logger().setLevel(1)
        self.data_feed.logger().addHandler(self)
        self._time = int(time.time())
        self._interval_in_seconds = 300  # 5 minutes

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mocking_assistant = NetworkMockingAssistant()
        self.resume_test_event = asyncio.Event()

    # -- Mock data methods required by TestCandlesBase -----------------------

    def _candles_data_mock(self):
        """
        Returns 4 candles in the standard 10-column format.
        Timestamps are spaced 300s (5m) apart.
        Last 4 columns are zero-filled (NonKYC doesn't provide those fields).
        """
        return [
            [self._time - self._interval_in_seconds * 3, 66934.0, 66951.8, 66800.0, 66901.6, 28.502, 0, 0, 0, 0],
            [self._time - self._interval_in_seconds * 2, 66901.7, 66989.3, 66551.7, 66669.9, 53.137, 0, 0, 0, 0],
            [self._time - self._interval_in_seconds * 1, 66669.9, 66797.5, 66595.1, 66733.4, 40.084, 0, 0, 0, 0],
            [self._time - self._interval_in_seconds * 0, 66733.4, 66757.4, 66550.0, 66575.4, 21.058, 0, 0, 0, 0],
        ]

    def get_fetch_candles_data_mock(self):
        """Returns same 4 candles — used by TestCandlesBase.test_fetch_candles for row count check."""
        return self._candles_data_mock()

    def get_candles_rest_data_mock(self):
        """
        Mock REST response from GET /market/candles in NonKYC's native format:
        {"bars": [{"time": <unix_sec>, "open": N, "high": N, "low": N, "close": N, "volume": N}], "meta": {...}}
        """
        return {
            "bars": [
                {
                    "time": self._time - self._interval_in_seconds * 3,
                    "open": 66934.0,
                    "high": 66951.8,
                    "low": 66800.0,
                    "close": 66901.6,
                    "volume": 28.502,
                },
                {
                    "time": self._time - self._interval_in_seconds * 2,
                    "open": 66901.7,
                    "high": 66989.3,
                    "low": 66551.7,
                    "close": 66669.9,
                    "volume": 53.137,
                },
                {
                    "time": self._time - self._interval_in_seconds * 1,
                    "open": 66669.9,
                    "high": 66797.5,
                    "low": 66595.1,
                    "close": 66733.4,
                    "volume": 40.084,
                },
                {
                    "time": self._time - self._interval_in_seconds * 0,
                    "open": 66733.4,
                    "high": 66757.4,
                    "low": 66550.0,
                    "close": 66575.4,
                    "volume": 21.058,
                },
            ],
            "meta": {"noData": False},
        }

    def get_candles_ws_data_mock_1(self):
        """
        First WS candle message — NonKYC JSON-RPC 2.0 updateCandles format.
        WS fields: timestamp (ISO8601), open, close, min, max, volume (all strings).
        NOTE: NonKYC uses 'min'/'max' not 'low'/'high'.
        """
        ts_1 = self._time - self._interval_in_seconds
        dt_1 = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts_1))
        return {
            "jsonrpc": "2.0",
            "method": "updateCandles",
            "params": {
                "data": [
                    {
                        "timestamp": dt_1,
                        "open": "66669.9",
                        "close": "66733.4",
                        "min": "66595.1",
                        "max": "66797.5",
                        "volume": "40.084",
                    }
                ],
                "symbol": "BTC/USDT",
                "period": 5,
            },
        }

    def get_candles_ws_data_mock_2(self):
        """
        Second WS candle message — MUST have a LATER timestamp than mock_1
        so TestCandlesBase.test_process_websocket_messages_with_two_valid_messages
        sees 2 distinct candles in the deque.
        """
        ts_2 = self._time
        dt_2 = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts_2))
        return {
            "jsonrpc": "2.0",
            "method": "updateCandles",
            "params": {
                "data": [
                    {
                        "timestamp": dt_2,
                        "open": "66733.4",
                        "close": "66575.4",
                        "min": "66550.0",
                        "max": "66757.4",
                        "volume": "21.058",
                    }
                ],
                "symbol": "BTC/USDT",
                "period": 5,
            },
        }

    def _success_subscription_mock(self):
        """NonKYC subscription ack is a JSON-RPC 2.0 result response."""
        return {"jsonrpc": "2.0", "result": True, "id": 1}

    # -- NonKYC-specific tests (beyond what TestCandlesBase provides) --------

    def test_trading_pair_conversion(self):
        """NonKYC uses slash-separated pairs: BTC-USDT -> BTC/USDT."""
        self.assertEqual(self.data_feed.get_exchange_trading_pair("BTC-USDT"), "BTC/USDT")
        self.assertEqual(self.data_feed.get_exchange_trading_pair("ETH-BTC"), "ETH/BTC")
        self.assertEqual(self.data_feed.get_exchange_trading_pair("DOGE-USDT"), "DOGE/USDT")

    def test_ex_trading_pair_set_on_init(self):
        """_ex_trading_pair should be 'BTC/USDT' after construction with 'BTC-USDT'."""
        self.assertEqual(self.data_feed._ex_trading_pair, "BTC/USDT")

    def test_rest_url(self):
        self.assertEqual(self.data_feed.rest_url, "https://api.nonkyc.io/api/v2")

    def test_wss_url(self):
        self.assertEqual(self.data_feed.wss_url, "wss://ws.nonkyc.io")

    def test_health_check_url(self):
        self.assertEqual(self.data_feed.health_check_url, "https://api.nonkyc.io/api/v2/time")

    def test_candles_url(self):
        self.assertEqual(self.data_feed.candles_url, "https://api.nonkyc.io/api/v2/market/candles")

    def test_intervals_supported(self):
        """NonKYC supports 5m through 1d but NOT 1m or 3m."""
        self.assertIn("5m", self.data_feed.intervals)
        self.assertIn("1h", self.data_feed.intervals)
        self.assertIn("1d", self.data_feed.intervals)
        self.assertNotIn("1m", self.data_feed.intervals)
        self.assertNotIn("3m", self.data_feed.intervals)

    def test_unsupported_interval_raises(self):
        """Attempting to create with 1m interval should raise (NonKYC minimum is 5m)."""
        with self.assertRaises(Exception):
            NonKYCSpotCandles(trading_pair="BTC-USDT", interval="1m")

    def test_rest_candles_params_basic(self):
        """Should build correct REST params with symbol, resolution, countBack."""
        params = self.data_feed._get_rest_candles_params()
        self.assertEqual(params["symbol"], "BTC/USDT")
        self.assertEqual(params["resolution"], 5)
        self.assertEqual(params["countBack"], 500)
        self.assertNotIn("from", params)
        self.assertNotIn("to", params)

    def test_rest_candles_params_with_times(self):
        """from/to should be included when start_time/end_time are given."""
        params = self.data_feed._get_rest_candles_params(start_time=1700000000, end_time=1700010000)
        self.assertEqual(params["from"], 1700000000)
        self.assertEqual(params["to"], 1700010000)

    def test_rest_candles_params_1h_resolution(self):
        """1h interval should produce resolution=60."""
        candles_1h = NonKYCSpotCandles(trading_pair="BTC-USDT", interval="1h")
        params = candles_1h._get_rest_candles_params()
        self.assertEqual(params["resolution"], 60)

    def test_rest_candles_params_1d_resolution(self):
        """1d interval should produce resolution=1440."""
        candles_1d = NonKYCSpotCandles(trading_pair="BTC-USDT", interval="1d")
        params = candles_1d._get_rest_candles_params()
        self.assertEqual(params["resolution"], 1440)

    def test_parse_rest_candles_empty_bars(self):
        """Empty bars list should return empty list."""
        self.assertEqual(self.data_feed._parse_rest_candles({"bars": []}), [])

    def test_parse_rest_candles_no_bars_key(self):
        """Missing 'bars' key should return empty list."""
        self.assertEqual(self.data_feed._parse_rest_candles({}), [])

    def test_parse_rest_candles_10_columns(self):
        """Each parsed candle should have exactly 10 columns."""
        data = {"bars": [{"time": 1700000000, "open": 42000.0, "high": 42100.0,
                          "low": 41900.0, "close": 42050.0, "volume": 100.0}]}
        result = self.data_feed._parse_rest_candles(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]), 10)

    def test_parse_rest_candles_zero_filled_fields(self):
        """Columns 6-9 should be 0.0 (NonKYC doesn't provide these)."""
        data = {"bars": [{"time": 1700000000, "open": 42000.0, "high": 42100.0,
                          "low": 41900.0, "close": 42050.0, "volume": 100.0}]}
        result = self.data_feed._parse_rest_candles(data)
        self.assertEqual(result[0][6], 0.0)  # quote_asset_volume
        self.assertEqual(result[0][7], 0.0)  # n_trades
        self.assertEqual(result[0][8], 0.0)  # taker_buy_base_volume
        self.assertEqual(result[0][9], 0.0)  # taker_buy_quote_volume

    def test_parse_rest_candles_millisecond_timestamp(self):
        """Should handle millisecond timestamps (convert to seconds)."""
        data = {"bars": [{"time": 1700000000000, "open": 1, "high": 2,
                          "low": 0.5, "close": 1.5, "volume": 10}]}
        result = self.data_feed._parse_rest_candles(data)
        self.assertEqual(result[0][0], 1700000000.0)

    def test_parse_rest_candles_string_values(self):
        """Should handle string-encoded prices (NonKYC precision format)."""
        data = {"bars": [{"time": 1700000000, "open": "42000.12345678",
                          "high": "42100.0", "low": "41900.0",
                          "close": "42050.0", "volume": "123.456"}]}
        result = self.data_feed._parse_rest_candles(data)
        self.assertAlmostEqual(result[0][1], 42000.12345678)

    def test_ws_subscription_payload(self):
        """Should produce a valid JSON-RPC 2.0 subscribeCandles payload."""
        payload = self.data_feed.ws_subscription_payload()
        self.assertEqual(payload["method"], "subscribeCandles")
        self.assertEqual(payload["params"]["symbol"], "BTC/USDT")
        self.assertEqual(payload["params"]["period"], 5)
        self.assertIn("id", payload)

    def test_ws_subscription_payload_1h(self):
        """Period should be 60 for 1h interval."""
        candles = NonKYCSpotCandles(trading_pair="ETH-BTC", interval="1h")
        payload = candles.ws_subscription_payload()
        self.assertEqual(payload["params"]["period"], 60)
        self.assertEqual(payload["params"]["symbol"], "ETH/BTC")

    def test_parse_ws_message_subscription_ack_ignored(self):
        """Subscription confirmation should return None."""
        msg = {"jsonrpc": "2.0", "result": True, "id": 1}
        self.assertIsNone(self.data_feed._parse_websocket_message(msg))

    def test_parse_ws_message_none_input(self):
        """None should return None."""
        self.assertIsNone(self.data_feed._parse_websocket_message(None))

    def test_parse_ws_message_unknown_method(self):
        """Non-candle methods should return None."""
        msg = {"jsonrpc": "2.0", "method": "updateTrades", "params": {"data": []}}
        self.assertIsNone(self.data_feed._parse_websocket_message(msg))

    def test_parse_ws_message_empty_data(self):
        """Empty data array should return None."""
        msg = {"jsonrpc": "2.0", "method": "updateCandles",
               "params": {"data": [], "symbol": "BTC/USDT", "period": 5}}
        self.assertIsNone(self.data_feed._parse_websocket_message(msg))

    def test_parse_ws_message_snapshot_uses_first_entry(self):
        """snapshotCandles (newest-first) should use the first entry."""
        msg = {
            "jsonrpc": "2.0",
            "method": "snapshotCandles",
            "params": {
                "data": [
                    {"timestamp": "2024-01-15T12:00:00.000Z", "open": "42000.5",
                     "close": "42050.0", "min": "41900.0", "max": "42100.0", "volume": "123.456"},
                    {"timestamp": "2024-01-15T11:55:00.000Z", "open": "41950.0",
                     "close": "42000.5", "min": "41800.0", "max": "42000.5", "volume": "99.0"},
                ],
                "symbol": "BTC/USDT",
                "period": 5,
            },
        }
        result = self.data_feed._parse_websocket_message(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["open"], "42000.5")
        self.assertEqual(result["high"], "42100.0")   # mapped from max
        self.assertEqual(result["low"], "41900.0")     # mapped from min

    def test_parse_ws_candle_min_max_mapping(self):
        """WS candles use 'min'/'max' — verify they map to 'low'/'high'."""
        candle = {"timestamp": "2024-06-01T00:00:00Z", "open": "100",
                  "close": "110", "min": "95", "max": "115", "volume": "50"}
        result = self.data_feed._parse_ws_candle(candle)
        self.assertEqual(result["low"], "95")    # from min
        self.assertEqual(result["high"], "115")  # from max

    def test_parse_ws_candle_iso8601_z_suffix(self):
        """Should parse ISO8601 with 'Z' suffix correctly."""
        candle = {"timestamp": "2024-06-01T00:00:00Z", "open": "1",
                  "close": "2", "min": "0.5", "max": "2.5", "volume": "10"}
        result = self.data_feed._parse_ws_candle(candle)
        from datetime import datetime, timezone
        expected = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(result["timestamp"], expected)

    def test_parse_ws_candle_iso8601_milliseconds(self):
        """Should parse ISO8601 with .000Z millisecond suffix."""
        candle = {"timestamp": "2024-06-01T12:30:00.000Z", "open": "1",
                  "close": "2", "min": "0.5", "max": "2.5", "volume": "10"}
        result = self.data_feed._parse_ws_candle(candle)
        from datetime import datetime, timezone
        expected = datetime(2024, 6, 1, 12, 30, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(result["timestamp"], expected)

    def test_parse_ws_candle_zero_filled_fields(self):
        """WS candle output should zero-fill the 4 fields NonKYC doesn't provide."""
        candle = {"timestamp": "2024-01-01T00:00:00Z", "open": "1",
                  "close": "2", "min": "0.5", "max": "2.5", "volume": "10"}
        result = self.data_feed._parse_ws_candle(candle)
        self.assertEqual(result["quote_asset_volume"], 0.0)
        self.assertEqual(result["n_trades"], 0.0)
        self.assertEqual(result["taker_buy_base_volume"], 0.0)
        self.assertEqual(result["taker_buy_quote_volume"], 0.0)

    def test_candles_factory_registration(self):
        """NonKYC should be in CandlesFactory._candles_map."""
        from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
        self.assertIn("nonkyc", CandlesFactory._candles_map)
        self.assertEqual(CandlesFactory._candles_map["nonkyc"], NonKYCSpotCandles)

    def test_candles_factory_creates_instance(self):
        """CandlesFactory.get_candle should return a NonKYCSpotCandles instance."""
        from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
        from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
        config = CandlesConfig(connector="nonkyc", trading_pair="BTC-USDT", interval="5m")
        candle = CandlesFactory.get_candle(config)
        self.assertIsInstance(candle, NonKYCSpotCandles)
        candle.stop()
