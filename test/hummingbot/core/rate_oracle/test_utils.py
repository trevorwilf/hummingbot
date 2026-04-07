"""Tests for rate_oracle/utils.py — find_rate zero-division guards."""
from decimal import Decimal
from unittest import TestCase

from hummingbot.core.rate_oracle.utils import find_rate


class TestFindRate(TestCase):

    def test_find_rate_direct(self):
        prices = {"HBOT-USDT": Decimal("100")}
        self.assertEqual(Decimal("100"), find_rate(prices, "HBOT-USDT"))

    def test_find_rate_reverse(self):
        prices = {"USDT-HBOT": Decimal("100")}
        self.assertEqual(Decimal("0.01"), find_rate(prices, "HBOT-USDT"))

    def test_find_rate_reverse_zero_denominator(self):
        prices = {"USDT-HBOT": Decimal("0")}
        result = find_rate(prices, "HBOT-USDT")
        self.assertEqual(Decimal("0"), result)

    def test_find_rate_proxy(self):
        prices = {"HBOT-USDT": Decimal("100"), "USDT-GBP": Decimal("0.75")}
        result = find_rate(prices, "HBOT-GBP")
        self.assertEqual(Decimal("75"), result)

    def test_find_rate_common_denom_zero(self):
        prices = {"HBOT-USDT": Decimal("100"), "GBP-USDT": Decimal("0")}
        result = find_rate(prices, "HBOT-GBP")
        self.assertEqual(Decimal("0"), result)

    def test_find_rate_missing(self):
        prices = {"HBOT-USDT": Decimal("100")}
        result = find_rate(prices, "AAVE-BTC")
        self.assertEqual(Decimal("0"), result)

    def test_find_rate_same_base_quote(self):
        result = find_rate({}, "USDT-USDT")
        self.assertEqual(Decimal("1"), result)
