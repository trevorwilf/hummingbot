import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource


class ConcreteOrderBookDataSource(OrderBookTrackerDataSource):
    """Minimal concrete subclass for testing."""

    def __init__(self):
        super().__init__(trading_pairs=["BTC-USDT"])

    async def _connected_websocket_assistant(self):
        raise NotImplementedError

    async def _subscribe_channels(self, ws):
        pass

    async def _order_book_snapshot(self, trading_pair):
        return {}

    async def get_new_order_book(self, trading_pair):
        from hummingbot.core.data_type.order_book import OrderBook
        return OrderBook()

    async def listen_for_order_book_diffs(self, ev_loop, output):
        pass

    async def listen_for_order_book_snapshots(self, ev_loop, output):
        pass

    async def listen_for_trades(self, ev_loop, output):
        pass

    async def get_last_traded_prices(self, trading_pairs, domain=None):
        return {}

    async def subscribe_to_trading_pair(self, trading_pair):
        pass

    async def unsubscribe_from_trading_pair(self, trading_pair):
        pass


class ConcreteUserStreamDataSource(UserStreamTrackerDataSource):
    """Minimal concrete subclass for testing."""

    async def _connected_websocket_assistant(self):
        raise NotImplementedError

    async def _subscribe_channels(self, websocket_assistant):
        pass


class TestOrderBookDataSourceRetryBackoff(unittest.TestCase):

    def test_connection_error_has_backoff(self):
        """ConnectionError should trigger a 5.0 second sleep."""
        ds = ConcreteOrderBookDataSource()
        loop = asyncio.new_event_loop()
        call_count = 0

        async def mock_connected_ws():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Connection reset")
            raise asyncio.CancelledError()

        ds._connected_websocket_assistant = mock_connected_ws
        sleep_calls = []
        original_sleep = ds._sleep

        async def track_sleep(duration):
            sleep_calls.append(duration)

        ds._sleep = track_sleep
        ds._on_order_stream_interruption = AsyncMock()

        try:
            loop.run_until_complete(ds.listen_for_subscriptions())
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

        self.assertIn(5.0, sleep_calls, "ConnectionError should sleep for 5.0 seconds")

    def test_generic_error_sleep_matches_log(self):
        """Generic exception should sleep 5.0 seconds (matching the log message)."""
        ds = ConcreteOrderBookDataSource()
        loop = asyncio.new_event_loop()
        call_count = 0

        async def mock_connected_ws():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Something unexpected")
            raise asyncio.CancelledError()

        ds._connected_websocket_assistant = mock_connected_ws
        sleep_calls = []

        async def track_sleep(duration):
            sleep_calls.append(duration)

        ds._sleep = track_sleep
        ds._on_order_stream_interruption = AsyncMock()

        try:
            loop.run_until_complete(ds.listen_for_subscriptions())
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

        self.assertIn(5.0, sleep_calls, "Generic error should sleep for 5.0 seconds")


class TestUserStreamDataSourceRetryBackoff(unittest.TestCase):

    def test_connection_error_has_backoff(self):
        """ConnectionError should trigger a 5.0 second sleep."""
        ds = ConcreteUserStreamDataSource()
        loop = asyncio.new_event_loop()
        call_count = 0

        async def mock_connected_ws():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Connection reset")
            raise asyncio.CancelledError()

        ds._connected_websocket_assistant = mock_connected_ws
        sleep_calls = []

        async def track_sleep(duration):
            sleep_calls.append(duration)

        ds._sleep = track_sleep
        ds._on_user_stream_interruption = AsyncMock()

        try:
            loop.run_until_complete(ds.listen_for_user_stream(asyncio.Queue()))
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

        self.assertIn(5.0, sleep_calls, "ConnectionError should sleep for 5.0 seconds")

    def test_generic_error_sleep_matches_log(self):
        """Generic exception should sleep 5.0 seconds (matching the log message)."""
        ds = ConcreteUserStreamDataSource()
        loop = asyncio.new_event_loop()
        call_count = 0

        async def mock_connected_ws():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Something unexpected")
            raise asyncio.CancelledError()

        ds._connected_websocket_assistant = mock_connected_ws
        sleep_calls = []

        async def track_sleep(duration):
            sleep_calls.append(duration)

        ds._sleep = track_sleep
        ds._on_user_stream_interruption = AsyncMock()

        try:
            loop.run_until_complete(ds.listen_for_user_stream(asyncio.Queue()))
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

        self.assertIn(5.0, sleep_calls, "Generic error should sleep for 5.0 seconds")


if __name__ == "__main__":
    unittest.main()
