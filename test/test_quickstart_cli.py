import argparse
import unittest


def _parse_bool(value):
    """Copy of _parse_bool from bin/hummingbot_quickstart.py for testing."""
    if isinstance(value, bool):
        return value
    if value.lower() in ('true', '1', 'yes'):
        return True
    elif value.lower() in ('false', '0', 'no'):
        return False
    raise argparse.ArgumentTypeError(f"Boolean value expected, got '{value}'")


def _make_parser():
    """Create a minimal parser matching the headless flag from CmdlineParser."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless",
                        type=_parse_bool,
                        nargs='?',
                        const=True,
                        default=None,
                        help="Run in headless mode without CLI interface.")
    return parser


class TestQuickstartCLI(unittest.TestCase):
    """Test the --headless flag boolean parsing."""

    def _parse(self, args):
        return _make_parser().parse_args(args)

    def test_headless_flag_no_value(self):
        """--headless with no value should be True."""
        args = self._parse(["--headless"])
        self.assertTrue(args.headless)

    def test_headless_flag_true(self):
        """--headless true should be True."""
        args = self._parse(["--headless", "true"])
        self.assertTrue(args.headless)

    def test_headless_flag_false(self):
        """--headless false should be False."""
        args = self._parse(["--headless", "false"])
        self.assertFalse(args.headless)

    def test_headless_flag_zero(self):
        """--headless 0 should be False."""
        args = self._parse(["--headless", "0"])
        self.assertFalse(args.headless)

    def test_headless_flag_one(self):
        """--headless 1 should be True."""
        args = self._parse(["--headless", "1"])
        self.assertTrue(args.headless)

    def test_headless_not_set(self):
        """No --headless flag should be None."""
        args = self._parse([])
        self.assertIsNone(args.headless)

    def test_headless_flag_yes(self):
        """--headless yes should be True."""
        args = self._parse(["--headless", "yes"])
        self.assertTrue(args.headless)

    def test_headless_flag_no(self):
        """--headless no should be False."""
        args = self._parse(["--headless", "no"])
        self.assertFalse(args.headless)


class TestParseBoolFunction(unittest.TestCase):
    """Test the _parse_bool helper directly."""

    def test_parse_bool_true_values(self):
        self.assertTrue(_parse_bool("true"))
        self.assertTrue(_parse_bool("True"))
        self.assertTrue(_parse_bool("1"))
        self.assertTrue(_parse_bool("yes"))
        self.assertTrue(_parse_bool(True))

    def test_parse_bool_false_values(self):
        self.assertFalse(_parse_bool("false"))
        self.assertFalse(_parse_bool("False"))
        self.assertFalse(_parse_bool("0"))
        self.assertFalse(_parse_bool("no"))
        self.assertFalse(_parse_bool(False))

    def test_parse_bool_invalid(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_bool("maybe")

    def test_production_code_uses_parse_bool(self):
        """Verify the production code uses _parse_bool, not type=bool."""
        import inspect
        import sys
        from unittest.mock import MagicMock

        # Mock unavailable modules
        for mod in ('grp', 'pwd', 'appnope', 'ptpython', 'ptpython.repl'):
            if mod not in sys.modules:
                sys.modules[mod] = MagicMock()

        sys.path.insert(0, "bin")
        try:
            import importlib
            if 'hummingbot_quickstart' in sys.modules:
                mod = importlib.reload(sys.modules['hummingbot_quickstart'])
            else:
                mod = importlib.import_module('hummingbot_quickstart')

            source = inspect.getsource(mod.CmdlineParser.__init__)
            self.assertIn('_parse_bool', source,
                          "CmdlineParser should use _parse_bool, not type=bool")
            self.assertNotIn('type=bool', source,
                             "CmdlineParser should not use type=bool for --headless")
        finally:
            sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()
