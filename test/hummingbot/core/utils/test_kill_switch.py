import asyncio
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.core.utils.kill_switch import ActiveKillSwitch


class TestKillSwitch(unittest.TestCase):

    def setUp(self):
        self.trading_core = MagicMock()
        self.trading_core.calculate_profitability = AsyncMock(return_value=Decimal("0.0"))
        self.trading_core.shutdown = AsyncMock()
        self.trading_core.notify = MagicMock()
        self.kill_switch = ActiveKillSwitch(
            kill_switch_rate=Decimal("-10"),
            trading_core=self.trading_core
        )

    def test_stop_does_not_cancel_current_task(self):
        """When stop() is called from within the kill switch's own task, it should not cancel it."""
        loop = asyncio.new_event_loop()

        async def run_test():
            # Create a task that simulates the kill switch loop
            async def fake_loop():
                # Simulate: kill switch triggers → shutdown → stop_strategy → stop()
                self.kill_switch._started = True
                # Now call stop from within this task
                self.kill_switch.stop()
                # Task should NOT be cancelled
                return "completed"

            task = loop.create_task(fake_loop())
            self.kill_switch._check_profitability_task = task
            result = await task
            self.assertEqual(result, "completed")
            self.assertFalse(self.kill_switch._started)
            # Task should not have been cancelled
            self.assertFalse(task.cancelled())

        try:
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_stop_cancels_task_from_external_caller(self):
        """When stop() is called from a different task, it should cancel the profitability task."""
        loop = asyncio.new_event_loop()

        async def run_test():
            # Create a long-running task simulating the kill switch loop
            async def fake_loop():
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    raise

            task = loop.create_task(fake_loop())
            self.kill_switch._check_profitability_task = task
            self.kill_switch._started = True

            # Let the task start
            await asyncio.sleep(0.01)

            # Call stop from a different task context
            self.kill_switch.stop()

            self.assertFalse(self.kill_switch._started)
            # Let the event loop process the cancellation
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.assertTrue(task.cancelled())

        try:
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_kill_switch_shutdown_completes_fully(self):
        """Simulate the full shutdown path: kill switch triggers → shutdown → stop_strategy → stop()."""
        loop = asyncio.new_event_loop()

        async def run_test():
            shutdown_completed = False

            async def mock_shutdown():
                nonlocal shutdown_completed
                # Simulate shutdown calling stop_strategy which calls kill_switch.stop()
                self.kill_switch.stop()
                shutdown_completed = True

            self.trading_core.shutdown = mock_shutdown
            self.trading_core.calculate_profitability = AsyncMock(return_value=Decimal("-0.15"))

            ks = ActiveKillSwitch(
                kill_switch_rate=Decimal("-10"),
                trading_core=self.trading_core
            )

            async def run_check():
                await ks.check_profitability_loop()

            task = loop.create_task(run_check())
            ks._check_profitability_task = task
            ks._started = True

            # Wait for the loop to trigger and complete shutdown
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.CancelledError:
                self.fail("Task was cancelled — self-cancellation guard failed")

            self.assertTrue(shutdown_completed)

        try:
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_stop_when_no_task(self):
        """stop() should work safely when no task exists."""
        self.kill_switch._started = True
        self.kill_switch.stop()
        self.assertFalse(self.kill_switch._started)

    def test_stop_when_task_already_done(self):
        """stop() should handle an already-done task gracefully."""
        loop = asyncio.new_event_loop()

        async def run_test():
            async def instant():
                return

            task = loop.create_task(instant())
            await task
            self.kill_switch._check_profitability_task = task
            self.kill_switch._started = True
            self.kill_switch.stop()
            self.assertFalse(self.kill_switch._started)

        try:
            loop.run_until_complete(run_test())
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
