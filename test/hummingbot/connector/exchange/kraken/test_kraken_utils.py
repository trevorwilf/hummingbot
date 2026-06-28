import unittest

from hummingbot.connector.exchange.kraken import kraken_utils as utils


class KrakenUtilTestCases(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "XBT"
        cls.hb_base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.hb_base_asset}-{cls.quote_asset}"
        cls.hb_trading_pair = f"{cls.hb_base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}{cls.quote_asset}"
        cls.ex_ws_trading_pair = f"{cls.base_asset}/{cls.quote_asset}"

    def test_convert_from_exchange_symbol(self):
        self.assertEqual(self.hb_base_asset, utils.convert_from_exchange_symbol(self.base_asset))
        self.assertEqual(self.quote_asset, utils.convert_from_exchange_symbol(self.quote_asset))

    def test_convert_to_exchange_symbol(self):
        self.assertEqual(self.base_asset, utils.convert_to_exchange_symbol(self.hb_base_asset))
        self.assertEqual(self.quote_asset, utils.convert_to_exchange_symbol(self.quote_asset))

    def test_convert_to_exchange_trading_pair(self):
        self.assertEqual(self.ex_trading_pair, utils.convert_to_exchange_trading_pair(self.hb_trading_pair))
        self.assertEqual(self.ex_trading_pair, utils.convert_to_exchange_trading_pair(self.ex_ws_trading_pair))
        self.assertEqual(self.ex_trading_pair, utils.convert_to_exchange_trading_pair(self.ex_trading_pair))

    def test_split_to_base_quote(self):
        self.assertEqual((self.hb_base_asset, self.quote_asset), utils.split_to_base_quote(self.trading_pair))

    def test_convert_from_exchange_trading_pair(self):
        self.assertEqual(self.trading_pair, utils.convert_from_exchange_trading_pair(self.trading_pair))
        self.assertEqual(self.trading_pair,
                         utils.convert_from_exchange_trading_pair(self.ex_trading_pair, ("BTC-USDT", "ETH-USDT")))
        self.assertEqual(self.trading_pair, utils.convert_from_exchange_trading_pair(self.ex_ws_trading_pair))

    def test_build_rate_limits_by_tier(self):
        rate_limits = utils.build_rate_limits_by_tier()
        self.assertIsNotNone(rate_limits)
        limit_ids = {rl.limit_id for rl in rate_limits}
        # The pools and the order-placement matching-engine limit must be present.
        self.assertIn("PrivateEndpointLimitID", limit_ids)
        self.assertIn("MatchingEngineLimitID", limit_ids)
        self.assertIn("/0/private/AddOrder", limit_ids)

    def test_convert_from_exchange_symbol_strips_only_legacy_prefixes(self):
        # Legacy prefixed codes ARE stripped (and remapped where applicable).
        self.assertEqual("BTC", utils.convert_from_exchange_symbol("XXBT"))
        self.assertEqual("USD", utils.convert_from_exchange_symbol("ZUSD"))
        self.assertEqual("ETH", utils.convert_from_exchange_symbol("XETH"))
        self.assertEqual("EUR", utils.convert_from_exchange_symbol("ZEUR"))
        self.assertEqual("DOGE", utils.convert_from_exchange_symbol("XXDG"))
        # Modern 4-letter tickers that merely start with X/Z must NOT be stripped (UTILSCFG-1 regression).
        self.assertEqual("ZEUS", utils.convert_from_exchange_symbol("ZEUS"))
        self.assertEqual("XAUT", utils.convert_from_exchange_symbol("XAUT"))
        self.assertEqual("ZETA", utils.convert_from_exchange_symbol("ZETA"))
        self.assertEqual("XCAD", utils.convert_from_exchange_symbol("XCAD"))
        # Shorter codes are untouched.
        self.assertEqual("XTZ", utils.convert_from_exchange_symbol("XTZ"))

    def test_convert_from_exchange_symbol_empty_string(self):
        # BAL-8: empty asset code must not raise IndexError.
        self.assertEqual("", utils.convert_from_exchange_symbol(""))

    def test_convert_from_exchange_trading_pair_without_available_pairs(self):
        # UTILSCFG-2: a bare token with no available pairs returns None instead of raising TypeError.
        self.assertIsNone(utils.convert_from_exchange_trading_pair("XBTUSDT"))
        self.assertIsNone(utils.convert_from_exchange_trading_pair("XBTUSDT", ()))

    def test_api_tier_validation(self):
        from pydantic import ValidationError

        from hummingbot.connector.exchange.kraken.kraken_utils import KrakenConfigMap
        with self.assertRaises(ValidationError):
            KrakenConfigMap(kraken_api_key="k", kraken_secret_key="s", kraken_api_tier="NotATier")
        with self.assertRaises(ValidationError):  # UTILSCFG-8: non-string input must not AttributeError
            KrakenConfigMap(kraken_api_key="k", kraken_secret_key="s", kraken_api_tier=123)
        cfg = KrakenConfigMap(kraken_api_key="k", kraken_secret_key="s", kraken_api_tier="Pro")
        self.assertEqual("Pro", cfg.kraken_api_tier)
