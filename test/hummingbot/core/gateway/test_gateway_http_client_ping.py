"""Gateway ping-failure logging tests (the 2026-07-12 hummingbot_us log flood).

A stack WITHOUT a Gateway container has no client certificates (they are generated
when a Gateway first starts), so GatewayHttpClient's status monitor — which pings
every POLL_INTERVAL (2s) forever — logged an ERROR with a full traceback thirty
times a minute, from a bare "[Errno 2] No such file or directory".

Under test:
- _http_client raises a DESCRIPTIVE FileNotFoundError naming the missing cert files
- ping_gateway logs the FIRST failure of an episode only: a WARNING (no traceback)
  for missing certs, an ERROR for anything else; repeats go to DEBUG
- a successful ping after failures logs an INFO recovery and re-arms the latch
"""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient

LOGGER_NAME = "hummingbot.core.gateway.gateway_http_client"


class _RecordCatcher(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def by_level(self, level):
        return [r for r in self.records if r.levelno == level]


class _PingHarness(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        # Reset the singleton + shared session so each test builds a fresh client.
        GatewayHttpClient._GatewayHttpClient__instance = None
        GatewayHttpClient._shared_client = None
        self.addCleanup(setattr, GatewayHttpClient, "_GatewayHttpClient__instance", None)
        self.addCleanup(setattr, GatewayHttpClient, "_shared_client", None)

        config = MagicMock()
        config.gateway_api_host = "localhost"
        config.gateway_api_port = "15888"
        config.gateway_use_ssl = True
        self.client = GatewayHttpClient.get_instance(config)

        self.catcher = _RecordCatcher()
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self.catcher)
        self.addCleanup(logger.removeHandler, self.catcher)


class TestMissingCertsQuietPing(_PingHarness):

    def setUp(self):
        super().setUp()
        # root_path() -> an empty temp dir: no certs exist, like a no-Gateway stack.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch("hummingbot.root_path", return_value=Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_http_client_names_the_missing_files(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            GatewayHttpClient._http_client(self.client._gateway_config)
        message = str(ctx.exception)
        self.assertIn("ca_cert.pem", message)
        self.assertIn("client_cert.pem", message)
        self.assertIn("client_key.pem", message)
        self.assertIn("generated when a Gateway first starts", message)

    async def test_missing_certs_warn_once_then_debug(self):
        for _ in range(10):
            self.assertFalse(await self.client.ping_gateway())
        warnings = self.catcher.by_level(logging.WARNING)
        errors = self.catcher.by_level(logging.ERROR)
        debugs = [r for r in self.catcher.by_level(logging.DEBUG)
                  if "still failing" in r.getMessage()]
        self.assertEqual(1, len(warnings), [r.getMessage() for r in warnings])
        self.assertIn("certificates not found", warnings[0].getMessage())
        self.assertEqual([], errors)          # a no-Gateway stack is not an ERROR
        self.assertEqual(9, len(debugs))      # repeats stay quiet

    async def test_recovery_logs_info_and_rearms_the_latch(self):
        await self.client.ping_gateway()      # failure #1 -> the one WARNING
        self.assertEqual(1, len(self.catcher.by_level(logging.WARNING)))

        with patch.object(self.client, "api_request", new=AsyncMock(return_value={"status": "ok"})):
            self.assertTrue(await self.client.ping_gateway())
        infos = [r for r in self.catcher.by_level(logging.INFO) if "recovered" in r.getMessage()]
        self.assertEqual(1, len(infos))

        # The latch is re-armed: a NEW failure episode logs loudly once more.
        await self.client.ping_gateway()
        self.assertEqual(2, len(self.catcher.by_level(logging.WARNING)))


class TestOtherFailuresStillLoud(_PingHarness):

    async def test_non_cert_failure_errors_once_then_debug(self):
        boom = ConnectionError("connection refused")
        with patch.object(self.client, "api_request", new=AsyncMock(side_effect=boom)):
            for _ in range(5):
                self.assertFalse(await self.client.ping_gateway())
        errors = self.catcher.by_level(logging.ERROR)
        debugs = [r for r in self.catcher.by_level(logging.DEBUG)
                  if "still failing" in r.getMessage()]
        self.assertEqual(1, len(errors))
        self.assertIn("Failed to ping gateway", errors[0].getMessage())
        self.assertEqual(4, len(debugs))


if __name__ == "__main__":
    unittest.main()
