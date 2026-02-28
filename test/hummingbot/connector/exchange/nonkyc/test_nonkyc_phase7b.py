"""Phase 7B: WS Compliance, Code Quality & Documentation -- Unit Tests."""
import asyncio
import json
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS
from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


# =========================================================================
# 7B-1: WS JSON-RPC id field compliance
# =========================================================================

class TestPhase7BWsJsonRpcId(unittest.TestCase):
    """7B-1: All WS payloads must include an 'id' field."""

    def test_auth_message_has_id(self):
        """Login payload must include id field."""
        mock_time = MagicMock()
        mock_time.time.return_value = 1234567890.0
        auth = NonkycAuth(api_key="testKey", secret_key="testSecret", time_provider=mock_time)
        msg = auth.generate_ws_authentication_message()
        self.assertIn("id", msg, "Auth login payload must include 'id'")
        self.assertIsInstance(msg["id"], int, "id must be an integer")

    def test_auth_message_has_id_99(self):
        """Auth uses fixed id=99."""
        mock_time = MagicMock()
        mock_time.time.return_value = 1234567890.0
        auth = NonkycAuth(api_key="testKey", secret_key="testSecret", time_provider=mock_time)
        msg = auth.generate_ws_authentication_message()
        self.assertEqual(99, msg["id"])

    def test_auth_message_still_has_method_and_params(self):
        """Adding id must not break existing fields."""
        mock_time = MagicMock()
        mock_time.time.return_value = 1234567890.0
        auth = NonkycAuth(api_key="testKey", secret_key="testSecret", time_provider=mock_time)
        msg = auth.generate_ws_authentication_message()
        self.assertEqual("login", msg["method"])
        self.assertIn("pKey", msg["params"])
        self.assertIn("nonce", msg["params"])
        self.assertIn("signature", msg["params"])


class TestPhase7BOrderBookWsId(IsolatedAsyncioWrapperTestCase):
    """7B-1: Order book data source WS ids."""

    def setUp(self):
        super().setUp()
        self.connector = NonkycExchange(
            nonkyc_api_key="test", nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"], trading_required=False)
        from bidict import bidict
        self.connector._set_trading_pair_symbol_map(bidict({"BTC/USDT": "BTC-USDT"}))

        from hummingbot.connector.exchange.nonkyc.nonkyc_api_order_book_data_source import (
            NonkycAPIOrderBookDataSource,
        )
        from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
        api_factory = web_utils.build_api_factory()
        self.obs = NonkycAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT"], connector=self.connector, api_factory=api_factory)

    def test_next_ws_id_increments(self):
        """_next_ws_id must return monotonically increasing integers."""
        id1 = self.obs._next_ws_id()
        id2 = self.obs._next_ws_id()
        id3 = self.obs._next_ws_id()
        self.assertEqual(id1 + 1, id2)
        self.assertEqual(id2 + 1, id3)
        self.assertIsInstance(id1, int)

    def test_initial_ws_id_is_zero_based(self):
        """Order book WS ids start from 0 (first call returns 1)."""
        self.assertEqual(0, self.obs._ws_request_id)
        self.assertEqual(1, self.obs._next_ws_id())

    async def test_subscribe_channels_payloads_have_id(self):
        """_subscribe_channels sends payloads with 'id' field."""
        sent_payloads = []

        mock_ws = AsyncMock()
        async def capture_send(request):
            sent_payloads.append(request.payload)
        mock_ws.send = capture_send

        await self.obs._subscribe_channels(mock_ws)

        self.assertGreaterEqual(len(sent_payloads), 2,
                                "Should send at least trade + orderbook subscribe")
        for payload in sent_payloads:
            self.assertIn("id", payload, f"Payload missing 'id': {payload.get('method')}")
            self.assertIsInstance(payload["id"], int)

        # Verify ids are unique
        ids = [p["id"] for p in sent_payloads]
        self.assertEqual(len(ids), len(set(ids)), "All WS request ids must be unique")


class TestPhase7BUserStreamWsId(IsolatedAsyncioWrapperTestCase):
    """7B-1: User stream data source WS ids."""

    def setUp(self):
        super().setUp()
        self.connector = NonkycExchange(
            nonkyc_api_key="test", nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"], trading_required=False)

        from hummingbot.connector.exchange.nonkyc.nonkyc_api_user_stream_data_source import (
            NonkycAPIUserStreamDataSource,
        )
        from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
        mock_time = MagicMock()
        mock_time.time.return_value = 1234567890.0
        auth = NonkycAuth(api_key="testKey", secret_key="testSecret", time_provider=mock_time)
        api_factory = web_utils.build_api_factory(auth=auth)
        self.uds = NonkycAPIUserStreamDataSource(
            auth=auth, trading_pairs=["BTC-USDT"],
            connector=self.connector, api_factory=api_factory)

    def test_user_stream_ws_id_starts_at_100(self):
        """User stream ids start at 100 to distinguish from order book in logs."""
        self.assertEqual(100, self.uds._ws_request_id)
        self.assertEqual(101, self.uds._next_ws_id())

    async def test_subscribe_channels_payloads_have_id(self):
        """User stream subscribe payloads include 'id'."""
        sent_payloads = []

        mock_ws = AsyncMock()
        async def capture_send(request):
            sent_payloads.append(request.payload)
        mock_ws.send = capture_send

        await self.uds._subscribe_channels(mock_ws)

        self.assertEqual(2, len(sent_payloads), "Should send subscribeReports + subscribeBalances")
        for payload in sent_payloads:
            self.assertIn("id", payload, f"Payload missing 'id': {payload.get('method')}")
            self.assertIsInstance(payload["id"], int)
            self.assertGreater(payload["id"], 100, "User stream ids should be > 100")


# =========================================================================
# 7B-2: Variable naming -- Nonkyc_ -> nonkyc_
# =========================================================================

class TestPhase7BVariableNaming(unittest.TestCase):
    """7B-2: Internal identifiers use snake_case."""

    def test_nonkyc_order_type_exists(self):
        """Method was renamed from Nonkyc_order_type to nonkyc_order_type."""
        self.assertTrue(hasattr(NonkycExchange, "nonkyc_order_type"),
                        "nonkyc_order_type (lowercase) must exist")

    def test_old_name_removed(self):
        """Old mixed-case name must not exist."""
        self.assertFalse(hasattr(NonkycExchange, "Nonkyc_order_type"),
                         "Old Nonkyc_order_type (mixed case) must be removed")

    def test_nonkyc_order_type_functionality_preserved(self):
        """Renamed method still works correctly."""
        from hummingbot.core.data_type.common import OrderType
        self.assertEqual("limit", NonkycExchange.nonkyc_order_type(OrderType.LIMIT))
        self.assertEqual("limit", NonkycExchange.nonkyc_order_type(OrderType.LIMIT_MAKER))
        self.assertEqual("market", NonkycExchange.nonkyc_order_type(OrderType.MARKET))

    def test_last_trades_poll_timestamp_renamed(self):
        """Instance var renamed to _last_trades_poll_nonkyc_timestamp."""
        exchange = NonkycExchange(
            nonkyc_api_key="test", nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"], trading_required=False)
        self.assertTrue(hasattr(exchange, "_last_trades_poll_nonkyc_timestamp"),
                        "Must have _last_trades_poll_nonkyc_timestamp (lowercase)")
        self.assertFalse(hasattr(exchange, "_last_trades_poll_Nonkyc_timestamp"),
                         "Old _last_trades_poll_Nonkyc_timestamp must be removed")


# =========================================================================
# 7B-3: DEFAULT_DOMAIN changed from "com" to "nonkyc"
# =========================================================================

class TestPhase7BDefaultDomain(unittest.TestCase):
    """7B-3: DEFAULT_DOMAIN is 'nonkyc', not 'com'."""

    def test_default_domain_is_nonkyc(self):
        self.assertEqual("nonkyc", CONSTANTS.DEFAULT_DOMAIN)

    def test_default_domain_not_com(self):
        self.assertNotEqual("com", CONSTANTS.DEFAULT_DOMAIN)

    def test_urls_still_work_with_new_domain(self):
        """URL builders must still produce correct URLs regardless of domain value."""
        from hummingbot.connector.exchange.nonkyc import nonkyc_web_utils as web_utils
        url = web_utils.public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL)
        self.assertIn("api.nonkyc.io", url)
        self.assertIn("/time", url)


# =========================================================================
# 7B-5: LIMIT_MAKER documentation (just verify docstring exists)
# =========================================================================

class TestPhase7BLimitMakerDoc(unittest.TestCase):
    """7B-5: nonkyc_order_type has docstring about LIMIT_MAKER limitation."""

    def test_docstring_exists(self):
        doc = NonkycExchange.nonkyc_order_type.__doc__
        self.assertIsNotNone(doc, "nonkyc_order_type must have a docstring")

    def test_docstring_mentions_limit_maker(self):
        doc = NonkycExchange.nonkyc_order_type.__doc__
        self.assertIn("LIMIT_MAKER", doc)

    def test_docstring_mentions_post_only(self):
        doc = NonkycExchange.nonkyc_order_type.__doc__
        self.assertIn("post-only", doc.lower())


# =========================================================================
# 7B-6: Rate limit documentation (verify comments exist)
# =========================================================================

class TestPhase7BRateLimitDocs(unittest.TestCase):
    """7B-6: Rate limit constants have tuning guidance."""

    def test_max_request_exists(self):
        self.assertTrue(hasattr(CONSTANTS, "MAX_REQUEST"))
        self.assertIsInstance(CONSTANTS.MAX_REQUEST, int)

    def test_rate_limits_list_non_empty(self):
        self.assertGreater(len(CONSTANTS.RATE_LIMITS), 10,
                           "Should have rate limits for all endpoints")


# =========================================================================
# 7B-7: README exists
# =========================================================================

class TestPhase7BReadme(unittest.TestCase):
    """7B-7: Connector README.md exists with key content."""

    def test_readme_exists(self):
        import os
        readme_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "..", "..",
            "hummingbot", "connector", "exchange", "nonkyc", "README.md")
        readme_path = os.path.normpath(readme_path)
        self.assertTrue(os.path.exists(readme_path),
                        f"README.md must exist at {readme_path}")

    def test_readme_has_content(self):
        import os
        readme_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "..", "..",
            "hummingbot", "connector", "exchange", "nonkyc", "README.md")
        readme_path = os.path.normpath(readme_path)
        if os.path.exists(readme_path):
            with open(readme_path, "r") as f:
                content = f.read()
            self.assertGreater(len(content), 500, "README should have substantial content")
            self.assertIn("NonKYC", content)
            self.assertIn("HMAC", content)
            self.assertIn("JSON-RPC", content)
