"""MQTT performance-publisher re-arm tests (the 2026-07-13 'stopped-but-trading' bot).

The MQTT bridge autostart retries in the background with app._mqtt set to None
between attempts. StrategyV2Base used to check the bridge exactly ONCE at strategy
start: a bot whose bridge was mid-retry at that instant (NonKYC bot, 02:57:37-47
window) never wired its performance publisher — it published logs/heartbeats but
no performance reports, so the dashboard showed it as permanently "stopped" while
it traded normally.

Under test:
- bridge up at start -> armed immediately (legacy behavior preserved)
- bridge down at start -> armed by a later on_tick once the bridge appears
- armed against the CURRENT gateway -> subsequent ticks are a no-op
- `mqtt restart` (new gateway object) -> publisher re-armed onto the new bridge
- no application/singleton -> quiet no-op that never CREATES an application
- publisher-construction race -> retried on the next tick
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.client.hummingbot_application import HummingbotApplication
from hummingbot.connector.test_support.mock_paper_exchange import MockPaperExchange
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class TestMqttPerformancePublisherRearm(unittest.TestCase):

    def setUp(self):
        connector = MockPaperExchange()
        config = StrategyV2ConfigBase(script_file_name="test", controllers_config=[])
        with patch("asyncio.create_task", return_value=MagicMock()), \
                patch.object(StrategyV2Base, "listen_to_executor_actions",
                             return_value=AsyncMock()), \
                patch("hummingbot.strategy.strategy_v2_base.ExecutorOrchestrator"), \
                patch("hummingbot.strategy.strategy_v2_base.MarketDataProvider"), \
                patch("hummingbot.strategy.strategy_v2_base.MarketsRecorder"):
            self.strategy = StrategyV2Base({"mock_paper_exchange": connector}, config=config)
        self.strategy.market_data_provider.ready = False

        pub_patcher = patch("hummingbot.strategy.strategy_v2_base.ETopicPublisher")
        self.epub = pub_patcher.start()
        self.addCleanup(pub_patcher.stop)

        self._app_patcher = None

    def _install_app(self, mqtt):
        app = MagicMock()
        app._mqtt = mqtt
        patcher = patch.object(HummingbotApplication, "_main_app", app)
        patcher.start()
        self.addCleanup(patcher.stop)
        return app

    def _gateway(self):
        gateway = MagicMock(name="gateway")
        pub_instance = MagicMock(name="publisher")
        pub_instance._gateway = gateway
        return gateway, pub_instance

    def test_bridge_up_at_start_arms_immediately(self):
        gateway, pub = self._gateway()
        self.epub.return_value = pub
        self._install_app(mqtt=gateway)
        self.strategy.start(MagicMock(), 1000.0)
        self.assertTrue(self.strategy.mqtt_enabled)
        self.epub.assert_called_once_with("performance", use_bot_prefix=True)

    def test_bridge_down_at_start_arms_on_a_later_tick(self):
        app = self._install_app(mqtt=None)
        self.strategy.start(MagicMock(), 1000.0)
        self.assertFalse(self.strategy.mqtt_enabled)
        self.assertIsNone(self.strategy._pub)

        # The autostart retry loop finally connects the bridge.
        gateway, pub = self._gateway()
        self.epub.return_value = pub
        app._mqtt = gateway
        self.strategy.on_tick()
        self.assertTrue(self.strategy.mqtt_enabled)
        self.assertIs(pub, self.strategy._pub)

        # Armed against the current gateway: further ticks do not rebuild it.
        self.strategy.on_tick()
        self.strategy.on_tick()
        self.assertEqual(1, self.epub.call_count)

    def test_mqtt_restart_rearms_onto_the_new_gateway(self):
        gateway1, pub1 = self._gateway()
        self.epub.return_value = pub1
        app = self._install_app(mqtt=gateway1)
        self.strategy.start(MagicMock(), 1000.0)
        self.assertIs(pub1, self.strategy._pub)

        # `mqtt restart` replaces the gateway object entirely: the old publisher
        # would enqueue into a dead bridge forever.
        gateway2, pub2 = self._gateway()
        self.epub.return_value = pub2
        app._mqtt = gateway2
        self.strategy.on_tick()
        self.assertIs(pub2, self.strategy._pub)
        self.assertEqual(2, self.epub.call_count)

    def test_no_application_is_a_quiet_noop_and_never_creates_one(self):
        patcher = patch.object(HummingbotApplication, "_main_app", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.strategy.start(MagicMock(), 1000.0)
        self.strategy.on_tick()
        self.assertFalse(self.strategy.mqtt_enabled)
        self.assertIsNone(HummingbotApplication._main_app)  # no accidental singleton

    def test_publisher_construction_race_is_retried_next_tick(self):
        gateway, pub = self._gateway()
        self._install_app(mqtt=gateway)
        # The bridge disappears between the None-check and the construct.
        self.epub.side_effect = [Exception("MQTT Gateway not yet initialized"), pub]
        self.strategy.start(MagicMock(), 1000.0)
        self.assertFalse(self.strategy.mqtt_enabled)
        self.assertIsNone(self.strategy._pub)

        self.strategy.on_tick()
        self.assertTrue(self.strategy.mqtt_enabled)
        self.assertIs(pub, self.strategy._pub)


if __name__ == "__main__":
    unittest.main()
