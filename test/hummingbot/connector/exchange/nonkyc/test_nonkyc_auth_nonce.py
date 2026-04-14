"""Tests for NonKYC monotonic nonce generation."""
import asyncio
import time
import unittest
from unittest.mock import MagicMock

from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth


class TestNonkycNonce(unittest.TestCase):
    """Verify nonces are strictly monotonically increasing under all conditions."""

    def setUp(self):
        self.time_provider = MagicMock()
        self.auth = NonkycAuth(
            api_key="test_key",
            secret_key="test_secret",
            time_provider=self.time_provider,
        )

    def test_sequential_nonces_are_strictly_increasing(self):
        """1000 rapid calls with same timestamp still produce unique increasing nonces."""
        self.time_provider.time.return_value = 1000.000  # fixed clock
        nonces = []
        for _ in range(1000):
            headers = self.auth.header_for_authentication("https://example.com")
            nonces.append(int(headers["X-API-NONCE"]))
        for i in range(1, len(nonces)):
            self.assertGreater(nonces[i], nonces[i - 1],
                               f"Nonce regression at index {i}: {nonces[i]} <= {nonces[i-1]}")

    def test_clock_regression_does_not_regress_nonce(self):
        """If the clock goes backward, nonces still increase."""
        self.time_provider.time.return_value = 2000.000
        h1 = self.auth.header_for_authentication("data1")
        nonce1 = int(h1["X-API-NONCE"])

        self.time_provider.time.return_value = 1999.000
        h2 = self.auth.header_for_authentication("data2")
        nonce2 = int(h2["X-API-NONCE"])

        self.assertGreater(nonce2, nonce1)

    def test_normal_clock_advance_uses_real_time(self):
        """When clock advances normally, nonce follows real time."""
        self.time_provider.time.return_value = 1000.000
        h1 = self.auth.header_for_authentication("data1")
        nonce1 = int(h1["X-API-NONCE"])

        self.time_provider.time.return_value = 1001.000
        h2 = self.auth.header_for_authentication("data2")
        nonce2 = int(h2["X-API-NONCE"])

        self.assertEqual(nonce1, 1000000)
        self.assertEqual(nonce2, 1001000)

    def test_concurrent_async_nonces_are_unique(self):
        """Multiple concurrent coroutines never produce duplicate nonces."""
        self.time_provider.time.return_value = 1000.000

        async def get_nonce():
            headers = self.auth.header_for_authentication("https://example.com")
            return int(headers["X-API-NONCE"])

        async def run_concurrent():
            tasks = [get_nonce() for _ in range(200)]
            return await asyncio.gather(*tasks)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(run_concurrent())
        finally:
            loop.close()

        self.assertEqual(len(results), len(set(results)),
                         f"Duplicate nonces found in {len(results)} concurrent calls")
        for i in range(1, len(results)):
            self.assertGreater(results[i], results[i - 1])

    def test_signature_changes_with_nonce(self):
        """Different nonces produce different signatures for same data."""
        self.time_provider.time.return_value = 1000.000
        h1 = self.auth.header_for_authentication("same_data")
        h2 = self.auth.header_for_authentication("same_data")
        self.assertNotEqual(h1["X-API-NONCE"], h2["X-API-NONCE"])
        self.assertNotEqual(h1["X-API-SIGN"], h2["X-API-SIGN"])


if __name__ == "__main__":
    unittest.main()
