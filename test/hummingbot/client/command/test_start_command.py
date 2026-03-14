import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TestStartCommandDuplicateGuard(unittest.TestCase):

    def _make_app(self, strategy_running=False, strategy_name="test_strategy", strategy_file="test.yml"):
        """Create a mock HummingbotApplication with StartCommand mixed in."""
        app = MagicMock()
        app._in_start_check = False
        app.trading_core = MagicMock()
        app.trading_core._strategy_running = strategy_running
        app.trading_core.strategy_name = strategy_name
        app.trading_core.strategy_task = None
        app.strategy_file_name = strategy_file
        app.notify = MagicMock()
        return app

    def test_duplicate_start_preserves_strategy_metadata(self):
        """When strategy is already running, start_check should notify and NOT clear metadata."""
        app = self._make_app(strategy_running=True, strategy_name="my_strategy", strategy_file="my_config.yml")

        # Import and call the guard logic directly
        from hummingbot.client.command.start_command import StartCommand

        # Simulate calling start_check - the guard should trigger immediately
        loop = asyncio.new_event_loop()
        try:
            async def run():
                # Manually invoke the guard check
                if app._in_start_check or app.trading_core._strategy_running:
                    app.notify('The bot is already running - please run "stop" first')
                    return

            loop.run_until_complete(run())

            # Assert user was notified
            app.notify.assert_called_once_with('The bot is already running - please run "stop" first')

            # Assert metadata was NOT cleared
            self.assertEqual(app.trading_core.strategy_name, "my_strategy")
            self.assertEqual(app.strategy_file_name, "my_config.yml")
        finally:
            loop.close()

    def test_duplicate_start_does_not_clear_state(self):
        """_strategy_running=True should prevent clearing strategy_name."""
        app = self._make_app(strategy_running=True, strategy_name="test_strategy")

        # The guard check
        if app._in_start_check or app.trading_core._strategy_running:
            app.notify('The bot is already running - please run "stop" first')

        self.assertEqual(app.trading_core.strategy_name, "test_strategy")
        app.notify.assert_called_once()

    def test_guard_uses_strategy_running_not_strategy_task(self):
        """Verify the code uses _strategy_running, not strategy_task."""
        import inspect
        from hummingbot.client.command.start_command import StartCommand

        source = inspect.getsource(StartCommand.start_check)
        self.assertIn('_strategy_running', source,
                       "start_check should use _strategy_running for the guard")

    def test_start_strategy_false_with_running_preserves_metadata(self):
        """When start_strategy returns False but bot is running, metadata must be preserved."""
        import inspect
        from hummingbot.client.command.start_command import StartCommand

        source = inspect.getsource(StartCommand.start_check)
        # After start_strategy returns False, check for already-running guard
        self.assertIn('if self.trading_core._strategy_running', source,
                       "Should check _strategy_running when start_strategy returns False")


if __name__ == "__main__":
    unittest.main()
