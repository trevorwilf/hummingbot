"""Test that MEXC fill recreation uses schema-aware fee types."""
import unittest
import inspect


class TestMexcFillRecreationFee(unittest.TestCase):

    def test_no_hardcoded_deducted_from_returns_in_fill_recreation(self):
        """The fill-recreation path must not hardcode DeductedFromReturnsTradeFee."""
        from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange
        source = inspect.getsource(MexcExchange)
        lines = source.split('\n')
        in_fill_recreation = False
        for line in lines:
            if 'Recreating missing trade in TradeFill' in line:
                in_fill_recreation = True
            if in_fill_recreation:
                break
            if 'trade_fee=DeductedFromReturnsTradeFee(' in line:
                self.fail(
                    "Found hardcoded DeductedFromReturnsTradeFee in fill recreation path. "
                    "Should use TradeFeeBase.new_spot_fee() instead."
                )

    def test_fill_recreation_uses_new_spot_fee(self):
        """The fill-recreation path should use TradeFeeBase.new_spot_fee."""
        from hummingbot.connector.exchange.mexc.mexc_exchange import MexcExchange
        source = inspect.getsource(MexcExchange._update_order_fills_from_trades)
        self.assertIn("new_spot_fee", source,
                      "Fill recreation should use TradeFeeBase.new_spot_fee()")


if __name__ == "__main__":
    unittest.main()
