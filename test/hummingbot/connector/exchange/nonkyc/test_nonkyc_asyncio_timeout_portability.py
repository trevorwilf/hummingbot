"""Phase 3 hardening: asyncio.timeout portability.

asyncio.timeout requires Python >= 3.11 but setup/environment.yml only guarantees a lower
bound; if conda ever resolved a lower interpreter, the CANCEL path would raise
AttributeError at runtime -- the worst possible failure point. Both former asyncio.timeout
usages (_place_cancel and _authenticate_ws_connection) now use async_timeout.timeout, which
raises asyncio.TimeoutError so all existing except clauses stay valid. cancel_all already
used async_timeout, so usage is now consistent.
"""
import asyncio
import inspect
import unittest
from decimal import Decimal
from typing import Awaitable
from unittest.mock import AsyncMock, MagicMock

from async_timeout import timeout as async_timeout_ctx

import hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source as user_stream_module
import hummingbot.connector.exchange.nonkyc.nonkyc_exchange as exchange_module
from hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source import NonkycAPIUserStreamDataSource
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState


class TestNoAsyncioTimeoutLiteral(unittest.TestCase):
    """Static guard: the two connector modules must not reference asyncio.timeout at all,
    so importing and running them never requires Python >= 3.11."""

    def test_exchange_module_has_no_asyncio_timeout(self):
        source = inspect.getsource(exchange_module)
        self.assertNotIn("asyncio.timeout(", source)

    def test_user_stream_module_has_no_asyncio_timeout(self):
        source = inspect.getsource(user_stream_module)
        self.assertNotIn("asyncio.timeout(", source)


class TestPlaceCancelTimeoutPath(unittest.TestCase):

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test_key",
            nonkyc_api_secret="test_secret",
            trading_pairs=["BTC-USDT"],
            trading_required=True,
        )

    def async_run(self, coro: Awaitable):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_place_cancel_timeout_returns_false_and_emits_event(self):
        # A hanging _api_post must be bounded by the (async_timeout) cancel timeout:
        # _place_cancel returns False and emits the cancel_request_timeout structured event.
        async def hanging_post(*args, **kwargs):
            await asyncio.sleep(60)

        self.exchange._api_post = hanging_post
        self.exchange._emit_structured_event = MagicMock()

        # Shrink the 15s window so the test runs fast, still through async_timeout.
        original_timeout = exchange_module.timeout
        exchange_module.timeout = lambda seconds: async_timeout_ctx(0.05)
        self.addCleanup(lambda: setattr(exchange_module, "timeout", original_timeout))

        order = InFlightOrder(
            client_order_id="HBOT-T1", exchange_order_id="EXCH-1",
            trading_pair="BTC-USDT", order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY, amount=Decimal("1"),
            price=Decimal("50000"), creation_timestamp=1700000000.0,
            initial_state=OrderState.OPEN,
        )
        result = self.async_run(self.exchange._place_cancel("HBOT-T1", order))

        self.assertFalse(result)
        timeout_events = [c for c in self.exchange._emit_structured_event.call_args_list
                          if c.args and c.args[0] == "cancel_request_timeout"]
        self.assertEqual(1, len(timeout_events))
        self.assertEqual("HBOT-T1", timeout_events[0].args[1]["order_id"])

    def test_place_cancel_success_path_unchanged(self):
        async def mock_post(path_url, data, **kwargs):
            return {"id": "CANCELLED"}

        self.exchange._api_post = mock_post
        order = InFlightOrder(
            client_order_id="HBOT-T2", exchange_order_id="EXCH-2",
            trading_pair="BTC-USDT", order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY, amount=Decimal("1"),
            price=Decimal("50000"), creation_timestamp=1700000000.0,
            initial_state=OrderState.OPEN,
        )
        self.assertTrue(self.async_run(self.exchange._place_cancel("HBOT-T2", order)))


class TestWsAuthTimeoutRetry(unittest.TestCase):

    def async_run(self, coro: Awaitable):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_ws_auth_retries_on_timeout_then_raises(self):
        # A silent socket must still time out (via async_timeout) and retry with backoff.
        auth = MagicMock()
        auth.generate_ws_authentication_message.return_value = {"id": 99, "method": "login"}
        data_source = NonkycAPIUserStreamDataSource(
            auth=auth,
            trading_pairs=["BTC-USDT"],
            connector=MagicMock(),
            api_factory=MagicMock(),
        )
        data_source._sleep = AsyncMock()  # skip the real backoff sleeps

        class SilentWebSocket:
            async def send(self, msg):
                pass

            async def iter_messages(self):
                await asyncio.sleep(3600)
                yield  # pragma: no cover -- never reached

        # Shrink the 10s/20s/30s windows so the test runs fast, still through async_timeout.
        original_timeout = user_stream_module.timeout
        user_stream_module.timeout = lambda seconds: async_timeout_ctx(0.05)
        self.addCleanup(lambda: setattr(user_stream_module, "timeout", original_timeout))

        with self.assertRaises(IOError) as ctx:
            self.async_run(data_source._authenticate_ws_connection(SilentWebSocket()))

        self.assertIn("after 3 attempts", str(ctx.exception))
        # Two backoff sleeps mean the timeout path retried twice before giving up.
        self.assertEqual(2, data_source._sleep.await_count)


if __name__ == "__main__":
    unittest.main()
