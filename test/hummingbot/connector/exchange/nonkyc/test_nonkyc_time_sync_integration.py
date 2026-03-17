"""
Integration tests for the NonKYC time synchronization + auth nonce pipeline.

Verifies that:
1. TimeSynchronizer correctly handles millisecond input from the time provider
2. NonkycAuth generates nonces in millisecond range using the synchronized time
3. A seconds-magnitude provider produces wildly wrong results (regression guard)
"""
import asyncio
import time
import unittest

from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
from hummingbot.connector.time_synchronizer import TimeSynchronizer


async def _ms_time_provider(value_ms: float):
    """Coroutine that returns a millisecond timestamp."""
    return value_ms


class TestTimeSyncIntegration(unittest.TestCase):

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_time_synchronizer_with_ms_provider(self):
        """TimeSynchronizer fed milliseconds should produce near-wall-clock seconds."""
        synchronizer = TimeSynchronizer()
        current_ms = time.time() * 1000

        # update_server_time_offset_with_time_provider expects an Awaitable (coroutine object)
        self._run(synchronizer.update_server_time_offset_with_time_provider(
            _ms_time_provider(current_ms)))

        # TimeSynchronizer.time() returns seconds
        synced_time = synchronizer.time()
        wall_clock = time.time()

        # Should be within 2 seconds of wall clock
        self.assertAlmostEqual(wall_clock, synced_time, delta=2.0)

    def test_auth_nonce_is_current_wall_clock_ms(self):
        """Auth nonce should be close to current time in milliseconds."""
        synchronizer = TimeSynchronizer()
        current_ms = time.time() * 1000

        self._run(synchronizer.update_server_time_offset_with_time_provider(
            _ms_time_provider(current_ms)))

        auth = NonkycAuth(
            api_key="test_key",
            secret_key="test_secret",
            time_provider=synchronizer,
        )

        headers = auth.header_for_authentication("test_data")
        nonce = int(headers["X-API-NONCE"])
        expected_ms = int(time.time() * 1000)

        # Nonce should be within 5 seconds of current wall-clock ms
        self.assertAlmostEqual(expected_ms, nonce, delta=5000)

    def test_auth_nonce_not_in_seconds(self):
        """Auth nonce must be in milliseconds range (> 1 trillion), not seconds."""
        synchronizer = TimeSynchronizer()
        current_ms = time.time() * 1000

        self._run(synchronizer.update_server_time_offset_with_time_provider(
            _ms_time_provider(current_ms)))

        auth = NonkycAuth(
            api_key="test_key",
            secret_key="test_secret",
            time_provider=synchronizer,
        )

        headers = auth.header_for_authentication("test_data")
        nonce = int(headers["X-API-NONCE"])

        # Must be in milliseconds range (> year 2001 in ms)
        self.assertGreater(nonce, 1_000_000_000_000)

    def test_time_synchronizer_rejects_seconds_magnitude(self):
        """If provider returns seconds instead of ms, synced time is wildly off.

        This documents the failure mode that the fix prevents. When the time
        provider returns seconds (~1.7 billion) instead of milliseconds
        (~1.7 trillion), TimeSynchronizer computes a massive negative offset,
        making .time() return a value far from wall-clock time.
        """
        synchronizer = TimeSynchronizer()
        # Simulate the old bug: return seconds instead of milliseconds
        current_seconds = time.time()

        self._run(synchronizer.update_server_time_offset_with_time_provider(
            _ms_time_provider(current_seconds)))  # WRONG: passing seconds as if ms

        synced_time = synchronizer.time()
        wall_clock = time.time()

        # The synced time should be wildly off (more than 1000 seconds)
        # because the offset was computed against a seconds value treated as ms
        drift = abs(synced_time - wall_clock)
        self.assertGreater(drift, 1000,
                           "Expected large drift when provider returns seconds instead of ms")


if __name__ == "__main__":
    unittest.main()
