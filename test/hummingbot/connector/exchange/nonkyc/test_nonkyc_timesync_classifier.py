"""Phase 2 hardening: REST timeouts must NOT be classified as time-sync/nonce errors.

The old classifier matched the bare substring "time", so "timeout" / "timed out" errors
were misclassified as clock/nonce errors. Each REST timeout then triggered
_on_nonce_error_detected() (a 2-second private-REST cooldown that blocks order placement)
plus a time-sync retry -- compounding latency exactly during exchange slowdowns.

The classifier now checks the timeout exclusion FIRST, then matches only genuine
time-sync signals: "nonce", "timestamp", "clock", and the standalone word "time"
(word-boundary regex).
"""
import unittest

from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


class TestTimesyncClassifierTimeoutExclusion(unittest.TestCase):

    def setUp(self):
        self.exchange = NonkycExchange(
            nonkyc_api_key="test",
            nonkyc_api_secret="test",
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        self.exchange._nonce_error_cooldown_until = 0.0

    def _classify(self, message: str) -> bool:
        return self.exchange._is_request_exception_related_to_time_synchronizer(Exception(message))

    # ---------------------------------------------------------- timeouts -> False, no cooldown

    def test_timed_out_is_not_time_sync(self):
        self.assertFalse(self._classify("Request timed out"))
        self.assertEqual(0.0, self.exchange._nonce_error_cooldown_until)

    def test_timeout_error_is_not_time_sync(self):
        self.assertFalse(self._classify("TimeoutError: timeout"))
        self.assertEqual(0.0, self.exchange._nonce_error_cooldown_until)

    def test_asyncio_timeout_repr_is_not_time_sync(self):
        self.assertFalse(self._classify("Timeout on reading data from socket"))
        self.assertEqual(0.0, self.exchange._nonce_error_cooldown_until)

    def test_timeout_exclusion_wins_even_with_time_word_present(self):
        # Exclusion is checked FIRST: a timeout message never sets the cooldown even when it
        # also contains a standalone "time" word.
        self.assertFalse(self._classify("time limit exceeded: request timed out"))
        self.assertEqual(0.0, self.exchange._nonce_error_cooldown_until)

    # -------------------------------------------------- genuine time-sync -> True + cooldown

    def _assert_true_with_cooldown(self, message: str):
        self.exchange._nonce_error_cooldown_until = 0.0
        self.assertTrue(self._classify(message))
        self.assertGreater(self.exchange._nonce_error_cooldown_until, 0.0)

    def test_invalid_nonce_is_time_sync(self):
        self._assert_true_with_cooldown("invalid nonce")

    def test_timestamp_too_old_is_time_sync(self):
        self._assert_true_with_cooldown("timestamp too old")

    def test_clock_skew_is_time_sync(self):
        self._assert_true_with_cooldown("clock skew detected")

    def test_server_time_mismatch_is_time_sync(self):
        self._assert_true_with_cooldown("server time mismatch")

    # ------------------------------------------------------------- word-boundary behavior

    def test_bare_time_substring_inside_other_words_is_not_time_sync(self):
        # "uptime"/"realtime" contain "time" but are not standalone words.
        self.assertFalse(self._classify("uptime check failed"))
        self.assertFalse(self._classify("realtime feed disconnected"))
        self.assertEqual(0.0, self.exchange._nonce_error_cooldown_until)

    def test_generic_errors_are_not_time_sync(self):
        self.assertFalse(self._classify("Unauthorized: invalid API key"))
        self.assertFalse(self._classify("Insufficient funds"))
        self.assertEqual(0.0, self.exchange._nonce_error_cooldown_until)


if __name__ == "__main__":
    unittest.main()
