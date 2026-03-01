import asyncio
import json
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, patch

from aioresponses import aioresponses

from hummingbot.connector.exchange.nonkyc import nonkyc_constants as CONSTANTS, nonkyc_web_utils as web_utils


class NonkycWebUtilsTests(IsolatedAsyncioWrapperTestCase):

    def test_public_rest_url(self):
        url = web_utils.public_rest_url(path_url=CONSTANTS.MARKETS_INFO_PATH_URL)
        self.assertEqual(f"{CONSTANTS.REST_URL}{CONSTANTS.API_VERSION}{CONSTANTS.MARKETS_INFO_PATH_URL}", url)

    def test_private_rest_url(self):
        url = web_utils.private_rest_url(path_url=CONSTANTS.USER_BALANCES_PATH_URL)
        self.assertEqual(f"{CONSTANTS.REST_URL}{CONSTANTS.API_VERSION}{CONSTANTS.USER_BALANCES_PATH_URL}", url)

    def test_build_api_factory(self):
        factory = web_utils.build_api_factory()
        self.assertIsNotNone(factory)

    @aioresponses()
    async def test_get_current_server_time(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL)
        response = {"serverTime": 1772170404982}
        mock_api.get(url, body=json.dumps(response))

        result = await web_utils.get_current_server_time()

        # Phase 2 Fix 5: milliseconds are now normalized to seconds
        self.assertAlmostEqual(1772170404.982, result, places=2)
