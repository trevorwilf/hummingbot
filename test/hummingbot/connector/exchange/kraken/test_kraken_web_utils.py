import unittest

import hummingbot.connector.exchange.kraken.kraken_constants as CONSTANTS
from hummingbot.connector.exchange.kraken import kraken_web_utils as web_utils


class KrakenUtilTestCases(unittest.TestCase):

    def test_public_rest_url(self):
        path_url = "/TEST_PATH"
        expected_url = CONSTANTS.BASE_URL + path_url
        self.assertEqual(expected_url, web_utils.public_rest_url(path_url))

    def test_private_rest_url(self):
        path_url = "/TEST_PATH"
        expected_url = CONSTANTS.BASE_URL + path_url
        self.assertEqual(expected_url, web_utils.private_rest_url(path_url))

    def test_is_exchange_information_valid(self):
        invalid_info_1 = {
            "XBTUSDT": {
                "altname": "XBTUSDT.d",
                "wsname": "XBT/USDT",
                "aclass_base": "currency",
                "base": "XXBT",
                "aclass_quote": "currency",
                "quote": "USDT",
            }
        }

        self.assertFalse(web_utils.is_exchange_information_valid(invalid_info_1["XBTUSDT"]))
        valid_info_1 = {
            "XBTUSDT": {
                "altname": "XBTUSDT",
                "wsname": "XBT/USDT",
                "aclass_base": "currency",
                "base": "XXBT",
                "aclass_quote": "currency",
                "quote": "USDT",
            }
        }

        self.assertTrue(web_utils.is_exchange_information_valid(valid_info_1["XBTUSDT"]))

    def test_is_exchange_information_valid_filters_non_online_status(self):
        # UTILSCFG-4 / BAL-3: only 'online' pairs are tradable; cancel_only / post_only are excluded,
        # and entries without a status field stay valid for back-compat.
        self.assertTrue(web_utils.is_exchange_information_valid({"altname": "XBTUSDT", "status": "online"}))
        self.assertFalse(web_utils.is_exchange_information_valid({"altname": "XBTUSDT", "status": "cancel_only"}))
        self.assertFalse(web_utils.is_exchange_information_valid({"altname": "XBTUSDT", "status": "post_only"}))
        self.assertTrue(web_utils.is_exchange_information_valid({"altname": "XBTUSDT"}))
        # Dark-pool filtering still applies even when the pair is online.
        self.assertFalse(web_utils.is_exchange_information_valid({"altname": "XBTUSDT.d", "status": "online"}))

    def test_rest_url_honours_domain(self):
        # UTILSCFG-10: the domain argument resolves through a mapping instead of being silently ignored.
        self.assertEqual(CONSTANTS.BASE_URL + "/x", web_utils.rest_url("/x", domain=CONSTANTS.DEFAULT_DOMAIN))
        self.assertEqual(CONSTANTS.BASE_URL + "/x", web_utils.rest_url("/x", domain="unknown-domain"))
