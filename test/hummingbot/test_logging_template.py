import unittest
from pathlib import Path
from ruamel.yaml import YAML

class TestLoggingTemplate(unittest.TestCase):
    def setUp(self):
        template_path = Path(__file__).parent.parent.parent / "hummingbot" / "templates" / "hummingbot_logs_TEMPLATE.yml"
        yaml = YAML()
        with open(template_path) as f:
            self.config = yaml.load(f)

    def test_forensic_file_handler_exists(self):
        self.assertIn("forensic_file_handler", self.config["handlers"])

    def test_forensic_handler_writes_to_instance_logs_dir(self):
        self.assertIn("$PROJECT_DIR/logs/", self.config["handlers"]["forensic_file_handler"]["filename"])

    def test_strategy_v2_namespaces_defined(self):
        for ns in ["hummingbot.strategy_v2", "hummingbot.strategy.strategy_v2_base",
                    "hummingbot.strategy_v2.controllers", "hummingbot.strategy_v2.executors"]:
            self.assertIn(ns, self.config["loggers"], f"Missing: {ns}")

    def test_budget_checker_namespace_defined(self):
        self.assertIn("hummingbot.connector.budget_checker", self.config["loggers"])

    def test_connector_namespaces_defined(self):
        self.assertIn("hummingbot.connector.exchange.nonkyc", self.config["loggers"])
        self.assertIn("hummingbot.connector.exchange.mexc", self.config["loggers"])

    def test_forensic_formatter_includes_line_numbers(self):
        fmt = self.config["formatters"]["forensic"]["format"]
        self.assertIn("%(funcName)s", fmt)
        self.assertIn("%(lineno)d", fmt)

    def test_strategy_v2_loggers_use_forensic_handler(self):
        for ns in ["hummingbot.strategy_v2", "hummingbot.strategy_v2.controllers"]:
            self.assertIn("forensic_file_handler", self.config["loggers"][ns]["handlers"])

    def test_strategy_v2_loggers_at_debug_level(self):
        for ns in ["hummingbot.strategy_v2", "hummingbot.strategy_v2.controllers"]:
            self.assertEqual(self.config["loggers"][ns]["level"], "DEBUG")
