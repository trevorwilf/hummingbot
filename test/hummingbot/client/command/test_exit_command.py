from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from hummingbot.client.command.exit_command import ExitCommand


class ExitCommandRecorderTests(IsolatedAsyncioWrapperTestCase):
    def setUp(self):
        super().setUp()
        self.recorder = Mock()
        self.core = SimpleNamespace(
            strategy=None,
            _strategy_running=False,
            _is_running=True,
            markets_recorder=self.recorder,
            cancel_outstanding_orders=AsyncMock(return_value=True),
            stop_clock=AsyncMock(return_value=True),
            gateway_monitor=None,
            notifiers=[],
        )
        self.app = SimpleNamespace(
            trading_core=self.core, notify=Mock(), app=Mock(), mqtt_stop=Mock()
        )
        sleep = patch("hummingbot.client.command.exit_command.asyncio.sleep", new_callable=AsyncMock)
        sleep.start()
        self.addCleanup(sleep.stop)

    async def test_successful_exit_keeps_recorder_until_cancellation_and_clock_stop(self):
        order = []

        async def cancel():
            self.assertIs(self.recorder, self.core.markets_recorder)
            self.recorder.stop.assert_not_called()
            order.append("cancel")
            return True

        async def stop_clock():
            self.recorder.stop.assert_not_called()
            order.append("clock")
            return True

        self.core.cancel_outstanding_orders.side_effect = cancel
        self.core.stop_clock.side_effect = stop_clock
        self.recorder.stop.side_effect = lambda: order.append("recorder")
        self.app.app.exit.side_effect = lambda: order.append("exit")

        await ExitCommand.exit_loop(self.app)

        self.assertEqual(["cancel", "clock", "recorder", "exit"], order)
        self.recorder.stop.assert_called_once_with()
        self.assertIsNone(self.core.markets_recorder)
        self.app.mqtt_stop.assert_called_once_with()

    async def test_failed_cancellation_aborts_exit_without_closing_recorder(self):
        self.core.cancel_outstanding_orders.return_value = False

        await ExitCommand.exit_loop(self.app)

        self.recorder.stop.assert_not_called()
        self.assertIs(self.recorder, self.core.markets_recorder)
        self.core.stop_clock.assert_not_awaited()
        self.app.app.exit.assert_not_called()
        self.app.mqtt_stop.assert_not_called()
        self.assertIn("Failed to cancel", self.app.notify.call_args.args[0])

    async def test_forced_exit_closes_recorder_without_canceling_orders(self):
        await ExitCommand.exit_loop(self.app, force=True)

        self.core.cancel_outstanding_orders.assert_not_awaited()
        self.recorder.stop.assert_called_once_with()
        self.assertIsNone(self.core.markets_recorder)
        self.app.app.exit.assert_called_once_with()

    async def test_exit_without_recorder_still_completes(self):
        self.core.markets_recorder = None

        await ExitCommand.exit_loop(self.app)

        self.recorder.stop.assert_not_called()
        self.app.app.exit.assert_called_once_with()
