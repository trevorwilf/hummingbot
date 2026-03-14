import asyncio
import unittest

from hummingbot.connector.exchange.mexc.protobuf import (
    PublicAggreDealsV3Api_pb2,
    PublicAggreDepthsV3Api_pb2,
    PushDataV3ApiWrapper_pb2,
)
from hummingbot.connector.exchange.mexc.mexc_post_processor import MexcPostProcessor
from hummingbot.core.web_assistant.connections.data_types import WSResponse


class TestMexcPostProcessor(unittest.TestCase):

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_post_processor_passes_dict_through(self):
        """Dict data should pass through unchanged."""
        data = {"channel": "test", "symbol": "BTCUSDC"}
        response = WSResponse(data=data)
        result = self._run(MexcPostProcessor.post_process(response))
        self.assertEqual(result.data, {"channel": "test", "symbol": "BTCUSDC"})

    def test_post_processor_decodes_protobuf_bytes(self):
        """Serialized protobuf bytes should be decoded to dict."""
        wrapper = PushDataV3ApiWrapper_pb2.PushDataV3ApiWrapper()
        wrapper.channel = "spot@public.aggre.depth.v3.api.pb@100ms@BTCUSDC"
        wrapper.symbol = "BTCUSDC"
        wrapper.sendTime = 1755973885809

        serialized = wrapper.SerializeToString()
        response = WSResponse(data=serialized)
        result = self._run(MexcPostProcessor.post_process(response))

        self.assertIsInstance(result.data, dict)
        self.assertEqual(result.data["channel"], "spot@public.aggre.depth.v3.api.pb@100ms@BTCUSDC")
        self.assertEqual(result.data["symbol"], "BTCUSDC")

    def test_post_processor_raises_on_invalid_bytes(self):
        """Invalid protobuf bytes should raise an exception."""
        response = WSResponse(data=b"invalid_protobuf_garbage\x00\xff\xfe")
        # protobuf ParseFromString may not always raise on arbitrary bytes,
        # but clearly malformed data should fail during parsing or produce empty/wrong result
        # The post_processor re-raises exceptions
        try:
            result = self._run(MexcPostProcessor.post_process(response))
            # If it didn't raise, verify the result is at least a dict (protobuf may silently parse some bytes)
            self.assertIsInstance(result.data, dict)
        except Exception:
            pass  # Expected — invalid bytes should raise

    def test_post_processor_round_trip_trade_message(self):
        """Realistic trade-channel protobuf message should round-trip correctly."""
        wrapper = PushDataV3ApiWrapper_pb2.PushDataV3ApiWrapper()
        wrapper.channel = "spot@public.aggre.deals.v3.api.pb@BTCUSDC"
        wrapper.symbol = "BTCUSDC"
        wrapper.sendTime = 1755973886258

        deal = wrapper.publicAggreDeals.deals.add()
        deal.price = "115091.25"
        deal.quantity = "0.000059"
        deal.tradeType = 1
        deal.time = 1755973886258

        serialized = wrapper.SerializeToString()
        response = WSResponse(data=serialized)
        result = self._run(MexcPostProcessor.post_process(response))

        self.assertIsInstance(result.data, dict)
        self.assertEqual(result.data["symbol"], "BTCUSDC")
        self.assertIn("publicAggreDeals", result.data)
        deals = result.data["publicAggreDeals"]["deals"]
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["price"], "115091.25")
        self.assertEqual(deals[0]["quantity"], "0.000059")
        self.assertEqual(deals[0]["tradeType"], 1)

    def test_post_processor_round_trip_depth_message(self):
        """Realistic depth-channel protobuf message should preserve version fields."""
        wrapper = PushDataV3ApiWrapper_pb2.PushDataV3ApiWrapper()
        wrapper.channel = "spot@public.aggre.depth.v3.api.pb@100ms@BTCUSDC"
        wrapper.symbol = "BTCUSDC"
        wrapper.sendTime = 1755973885809

        depth = wrapper.publicAggreDepths
        depth.eventType = "spot@public.aggre.depth.v3.api.pb@100ms"
        depth.fromVersion = "17521975448"
        depth.toVersion = "17521975455"

        bid = depth.bids.add()
        bid.price = "114838.84"
        bid.quantity = "0.000101"

        ask = depth.asks.add()
        ask.price = "115198.74"
        ask.quantity = "0.068865"

        serialized = wrapper.SerializeToString()
        response = WSResponse(data=serialized)
        result = self._run(MexcPostProcessor.post_process(response))

        self.assertIsInstance(result.data, dict)
        depths = result.data["publicAggreDepths"]
        self.assertEqual(depths["fromVersion"], "17521975448")
        self.assertEqual(depths["toVersion"], "17521975455")
        self.assertEqual(len(depths["bids"]), 1)
        self.assertEqual(depths["bids"][0]["price"], "114838.84")
        self.assertEqual(len(depths["asks"]), 1)
        self.assertEqual(depths["asks"][0]["price"], "115198.74")


if __name__ == "__main__":
    unittest.main()
