import logging
import unittest
from hummingbot.strategy_v2.runnable_base import RunnableBase

class ConcreteRunnableA(RunnableBase):
    _logger = None
    async def control_task(self):
        pass

class ConcreteRunnableB(RunnableBase):
    _logger = None
    async def control_task(self):
        pass

class TestRunnableBaseLogger(unittest.TestCase):
    def test_logger_uses_class_name_not_module(self):
        logger_a = ConcreteRunnableA.logger()
        logger_b = ConcreteRunnableB.logger()
        self.assertIn("ConcreteRunnableA", logger_a.name)
        self.assertIn("ConcreteRunnableB", logger_b.name)
        self.assertNotEqual(logger_a.name, logger_b.name)

    def test_logger_is_cached_per_class(self):
        logger1 = ConcreteRunnableA.logger()
        logger2 = ConcreteRunnableA.logger()
        self.assertIs(logger1, logger2)

    def test_different_classes_get_different_loggers(self):
        self.assertIsNot(ConcreteRunnableA.logger(), ConcreteRunnableB.logger())
