import unittest
from unittest.mock import MagicMock, patch

from hummingbot.remote_iface.messages import StartCommandMessage


class TestStartCommandMessageContract(unittest.TestCase):

    def test_start_command_message_has_v2_conf(self):
        """StartCommandMessage.Request must have v2_conf, not script/conf."""
        msg = StartCommandMessage.Request()
        self.assertTrue(hasattr(msg, 'v2_conf'), "Missing v2_conf field")
        self.assertFalse(hasattr(msg, 'script'), "Stale 'script' field still present")
        self.assertFalse(hasattr(msg, 'conf'), "Stale 'conf' field still present")

    def test_start_command_v2_conf_default_none(self):
        msg = StartCommandMessage.Request()
        self.assertIsNone(msg.v2_conf)

    def test_start_command_v2_conf_accepts_value(self):
        msg = StartCommandMessage.Request(v2_conf="my_config.yml")
        self.assertEqual(msg.v2_conf, "my_config.yml")


class TestMQTTCommandsStrategyAccess(unittest.TestCase):

    def test_mqtt_commands_strategy_access_path(self):
        """Verify strategy checks use trading_core.strategy, not _hb_app.strategy."""
        import inspect
        from hummingbot.remote_iface.mqtt import MQTTCommands

        source = inspect.getsource(MQTTCommands._on_cmd_start)
        self.assertIn('trading_core.strategy', source,
                       "_on_cmd_start should access strategy via trading_core.strategy")
        self.assertNotIn('self._hb_app.strategy is', source,
                         "_on_cmd_start should not use self._hb_app.strategy directly")

    def test_mqtt_commands_config_access_uses_public_property(self):
        """Verify config access uses public strategy_config_map property."""
        import inspect
        from hummingbot.remote_iface.mqtt import MQTTCommands

        source = inspect.getsource(MQTTCommands._on_cmd_config)
        self.assertNotIn('_strategy_config_map', source,
                         "_on_cmd_config should not use private _strategy_config_map")
        self.assertIn('strategy_config_map', source,
                       "_on_cmd_config should use public strategy_config_map property")

    def test_mqtt_start_passes_v2_conf(self):
        """Verify _on_cmd_start passes v2_conf to start()."""
        import inspect
        from hummingbot.remote_iface.mqtt import MQTTCommands

        source = inspect.getsource(MQTTCommands._on_cmd_start)
        self.assertIn('v2_conf=msg.v2_conf', source,
                       "_on_cmd_start should pass v2_conf=msg.v2_conf")
        self.assertNotIn('script=msg.script', source,
                         "_on_cmd_start should not pass script=msg.script")
        self.assertNotIn('conf=msg.conf', source,
                         "_on_cmd_start should not pass conf=msg.conf")


if __name__ == "__main__":
    unittest.main()
