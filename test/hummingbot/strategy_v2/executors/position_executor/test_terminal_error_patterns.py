"""Tests for terminal error pattern matching."""
import unittest
from unittest.mock import MagicMock

from hummingbot.strategy_v2.executors.position_executor.position_executor import PositionExecutor


class TestTerminalErrorPatterns(unittest.TestCase):

    def setUp(self):
        self.executor = MagicMock(spec=PositionExecutor)
        self.executor._TERMINAL_ERROR_PATTERNS = PositionExecutor._TERMINAL_ERROR_PATTERNS
        self.executor._is_terminal_failure = PositionExecutor._is_terminal_failure.__get__(
            self.executor, PositionExecutor
        )

    def _make_event(self, error_msg):
        event = MagicMock()
        event.error_message = error_msg
        return event

    def test_insufficient_funds_is_terminal(self):
        event = self._make_event("Order failed: Insufficient funds for this order")
        self.assertTrue(self.executor._is_terminal_failure(event))

    def test_oversold_is_terminal(self):
        event = self._make_event('{"msg":"Oversold","code":30005}')
        self.assertTrue(self.executor._is_terminal_failure(event))

    def test_mexc_code_30005_is_terminal(self):
        event = self._make_event("MEXC error 30005: order rejected")
        self.assertTrue(self.executor._is_terminal_failure(event))

    def test_nonkyc_code_20001_is_terminal(self):
        event = self._make_event("NonKYC error code 20001 - insufficient balance")
        self.assertTrue(self.executor._is_terminal_failure(event))

    def test_bad_nonce_is_terminal(self):
        event = self._make_event("Bad Nonce - request rejected")
        self.assertTrue(self.executor._is_terminal_failure(event))

    def test_minimum_order_size_still_terminal(self):
        """Original patterns still work."""
        event = self._make_event("Order amount is lower than minimum order size")
        self.assertTrue(self.executor._is_terminal_failure(event))

    def test_transient_error_is_not_terminal(self):
        event = self._make_event("Connection timeout - server unavailable")
        self.assertFalse(self.executor._is_terminal_failure(event))

    def test_503_is_not_terminal(self):
        event = self._make_event("status is 503 Unknown error, please check your request or try again later.")
        self.assertFalse(self.executor._is_terminal_failure(event))

    def test_none_error_message_is_not_terminal(self):
        event = self._make_event(None)
        self.assertFalse(self.executor._is_terminal_failure(event))


if __name__ == "__main__":
    unittest.main()
