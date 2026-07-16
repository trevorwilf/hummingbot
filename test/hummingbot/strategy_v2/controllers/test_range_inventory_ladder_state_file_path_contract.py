"""CONTRACT C1 -- state_file_name path contract (CDX-007 / CLA-004).

Every expectation here is derived from the CONTRACT C1 text, NOT from running the
implementation:

    ACCEPT: unset/None; or a `str` whose stripped value is non-empty and, parsed as BOTH
    PurePosixPath and PureWindowsPath: `is_absolute()` is False, has no drive and no
    root/anchor, contains no `..` component, is not `.`, and its POSIX normalization
    remains a strict descendant of `data/` when joined.
    REJECT (fail-closed): absolute POSIX or Windows paths, drive letters, UNC paths, any
    `..` component, `.` -- UNLESS an explicit opt-out boolean
    (`allow_absolute_state_file_name`, default False) is set, which permits ABSOLUTE paths
    only, never traversal.
    Canonical value: the stripped string. Empty-after-strip maps to unset/None.

The pre-fix validator mapped only `""` -> None and passed everything else through, and
`state_path` composed `Path("data") / file_name`, which honours an absolute right operand:
the ladder state file escaped `data/` silently. These tests cover both halves of the fix --
the lexical validator and the runtime containment assertion.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from pydantic import ValidationError

_CTRL_DIR = Path(__file__).resolve().parents[4] / "controllers" / "market_making"
if str(_CTRL_DIR) not in sys.path:
    sys.path.insert(0, str(_CTRL_DIR))

from test.hummingbot.strategy_v2.controllers.test_range_inventory_ladder_preflight_retry import (  # noqa: E402
    D,
    _make_mdp,
)

from range_inventory_ladder import (  # noqa: E402
    RangeInventoryLadderConfig,
    RangeInventoryLadderController,
)


def _config(**overrides) -> RangeInventoryLadderConfig:
    """A minimal VALID ladder config. Any ValidationError raised by a test case is therefore
    attributable to the field under test, not to an unrelated invalid field."""
    kwargs = dict(
        id="ctrl-c1",
        controller_name="range_inventory_ladder",
        controller_type="market_making",
        connector_name="nonkyc",
        trading_pair="DASH-USDT",
        total_amount_quote=Decimal("100"),
        buy_prices=[Decimal("23.0")],
        buy_amounts_pct=[Decimal("1")],
        sell_prices=[Decimal("25.5")],
        sell_amounts_pct=[Decimal("1")],
        min_order_quote=Decimal("1"),
    )
    kwargs.update(overrides)
    return RangeInventoryLadderConfig(**kwargs)


# C1 reject set. Each value is rejected for a reason C1 names explicitly.
REJECTED = {
    "posix_absolute": "/tmp/x.json",
    "posix_traversal": "../conf/x.yml",
    "windows_drive_absolute": "C:\\x.json",
    "windows_unc": "\\\\share\\x",
    "dot": ".",
}

# C1 accept set.
ACCEPTED = {
    "bare_file": "x.json",
    "nested_relative": "sub/dir/x.json",
}


class TestStateFileNameLexicalContract(unittest.TestCase):
    """The validator half of C1."""

    def test_control_valid_config_builds(self):
        """Guard against every rejection test below passing for the wrong reason: the base
        config with an accepted state_file_name must construct cleanly."""
        self.assertEqual("x.json", _config(state_file_name="x.json").state_file_name)

    def test_reject_set(self):
        for case, value in REJECTED.items():
            with self.subTest(case=case, value=value):
                with self.assertRaises(ValidationError) as ctx:
                    _config(state_file_name=value)
                # Prove OUR validator rejected it, and that it named the offending field.
                self.assertIn("state_file_name", str(ctx.exception))

    def test_accept_set(self):
        for case, value in ACCEPTED.items():
            with self.subTest(case=case, value=value):
                self.assertEqual(value, _config(state_file_name=value).state_file_name)

    def test_empty_string_maps_to_none(self):
        """C1: 'Empty-after-strip maps to unset/None (default behavior)'."""
        self.assertIsNone(_config(state_file_name="").state_file_name)

    def test_whitespace_only_maps_to_none(self):
        """C1: empty AFTER STRIP -> None. Note the deliberate asymmetry with CONTRACT C2,
        where whitespace-only `id` is REJECTED: for a file name None is a safe default
        (auto-generated name), for an identity it is not."""
        self.assertIsNone(_config(state_file_name="   ").state_file_name)

    def test_canonical_value_is_the_stripped_string(self):
        """C1: 'Canonical value: the stripped string.'"""
        self.assertEqual("x.json", _config(state_file_name="  x.json  ").state_file_name)

    def test_unset_is_accepted_as_none(self):
        self.assertIsNone(_config().state_file_name)

    def test_traversal_hidden_behind_a_leading_component_is_rejected(self):
        """`sub/../../x` normalizes out of data/. C1 rejects ANY `..` component, not just a
        leading one."""
        with self.assertRaises(ValidationError):
            _config(state_file_name="sub/../../x.json")

    def test_backslash_traversal_is_rejected(self):
        """PurePosixPath parses `a\\..\\b` as ONE opaque component and sees no traversal.
        C1 mandates parsing as BOTH flavors; the Windows flavor is what catches this."""
        with self.assertRaises(ValidationError):
            _config(state_file_name="sub\\..\\..\\x.json")

    def test_windows_rooted_but_driveless_path_is_rejected(self):
        """`/tmp/x.json` has no drive, so PureWindowsPath.is_absolute() is False. C1 requires
        'no drive AND no root/anchor' precisely so this cannot slip through an
        is_absolute()-only check."""
        with self.assertRaises(ValidationError):
            _config(state_file_name="/tmp/x.json")

    def test_windows_drive_relative_path_is_rejected(self):
        """`C:x.json` is drive-relative: is_absolute() is False under both flavors, but it
        HAS a drive. C1 rejects drive letters."""
        with self.assertRaises(ValidationError):
            _config(state_file_name="C:x.json")


class TestStateFileNameOptOut(unittest.TestCase):
    """C1: the opt-out 'permits ABSOLUTE paths only, never traversal'."""

    def test_opt_out_defaults_to_false(self):
        """Fail-closed default."""
        self.assertFalse(_config().allow_absolute_state_file_name)

    def test_opt_out_permits_absolute(self):
        config = _config(state_file_name="/abs/x.json", allow_absolute_state_file_name=True)
        self.assertEqual("/abs/x.json", config.state_file_name)

    def test_opt_out_still_rejects_traversal(self):
        with self.assertRaises(ValidationError):
            _config(state_file_name="../x", allow_absolute_state_file_name=True)

    def test_opt_out_still_rejects_relative_traversal_below_a_component(self):
        with self.assertRaises(ValidationError):
            _config(state_file_name="sub/../../x.json", allow_absolute_state_file_name=True)

    def test_opt_out_still_rejects_dot(self):
        """`.` is not absolute, so the opt-out does not reach it."""
        with self.assertRaises(ValidationError):
            _config(state_file_name=".", allow_absolute_state_file_name=True)

    def test_opt_out_permits_windows_absolute(self):
        config = _config(state_file_name="C:\\x.json", allow_absolute_state_file_name=True)
        self.assertEqual("C:\\x.json", config.state_file_name)

    def test_absolute_rejected_when_opt_out_absent(self):
        """The same value the opt-out permits must be rejected without it -- otherwise the
        opt-out is decorative."""
        with self.assertRaises(ValidationError):
            _config(state_file_name="/abs/x.json")


class TestDiagnosticLogFileNameContract(unittest.TestCase):
    """diagnostic_log_file_name shares the validator and composes an identical
    `Path("data") / name` at diagnostic_log_path -- the same escape. It gets C1's
    relative-only rules; the opt-out is named for the state file and is NOT honoured here."""

    def test_reject_set(self):
        for case, value in REJECTED.items():
            with self.subTest(case=case, value=value):
                with self.assertRaises(ValidationError) as ctx:
                    _config(diagnostic_log_file_name=value)
                self.assertIn("diagnostic_log_file_name", str(ctx.exception))

    def test_accept_set(self):
        for case, value in ACCEPTED.items():
            with self.subTest(case=case, value=value):
                self.assertEqual(value, _config(diagnostic_log_file_name=value).diagnostic_log_file_name)

    def test_empty_string_maps_to_none(self):
        self.assertIsNone(_config(diagnostic_log_file_name="").diagnostic_log_file_name)

    def test_state_file_opt_out_does_not_leak_to_the_diagnostic_log(self):
        """allow_absolute_state_file_name names the STATE file. It must not silently widen a
        second field's accept set."""
        with self.assertRaises(ValidationError):
            _config(diagnostic_log_file_name="/abs/x.jsonl", allow_absolute_state_file_name=True)


class TestRuntimeContainmentAssertion(unittest.TestCase):
    """The belt-and-braces half of C1: `Path("data") / name` honours an absolute right
    operand, so the use-site asserts containment and RAISES rather than proceeding.

    The lexical validator is bypassed here on purpose -- that is exactly the condition the
    runtime check exists for. Configs are mutated post-validation by the is_updatable
    machinery and can be built via `model_construct`, neither of which re-runs the field
    validator."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, self._old_cwd)

    def _controller(self, **overrides) -> RangeInventoryLadderController:
        balances = {"DASH": [D(0), D(0)], "USDT": [D(100), D(100)]}
        mdp = _make_mdp(balances=balances, mid=24.30, bid=24.29, ask=24.31)
        return RangeInventoryLadderController(
            _config(**overrides), market_data_provider=mdp, actions_queue=MagicMock()
        )

    @staticmethod
    def _bypass_validator(config, field: str, value):
        """Plant a value the validator would have rejected, without re-validating."""
        config.__dict__[field] = value

    def test_valid_relative_name_composes_under_data(self):
        """No false positive: the accepted case still returns data/<name>."""
        ctrl = self._controller(state_file_name="x.json")
        self.assertEqual(Path("data") / "x.json", ctrl.state_path)

    def test_absolute_state_file_name_planted_post_validation_raises(self):
        ctrl = self._controller(state_file_name="x.json")
        escaping = str(Path(self._tmp.name).parent / "escaped.json")
        self._bypass_validator(ctrl.config, "state_file_name", escaping)
        with self.assertRaises(ValueError) as ctx:
            ctrl.state_path
        self.assertIn("state_file_name", str(ctx.exception))

    def test_traversal_planted_post_validation_raises(self):
        ctrl = self._controller(state_file_name="x.json")
        self._bypass_validator(ctrl.config, "state_file_name", "../../escaped.json")
        with self.assertRaises(ValueError):
            ctrl.state_path

    def test_memoized_pass_does_not_mask_a_later_escape(self):
        """The containment result is cached to keep resolve() off the hot path. The cache key
        carries the composed path, so a LATER mutation to an escaping name must still raise
        rather than be served a stale pass."""
        ctrl = self._controller(state_file_name="x.json")
        self.assertEqual(Path("data") / "x.json", ctrl.state_path)  # populate the memo
        self._bypass_validator(ctrl.config, "state_file_name", "/tmp/escaped.json")
        with self.assertRaises(ValueError):
            ctrl.state_path

    def test_diagnostic_log_path_escape_raises(self):
        ctrl = self._controller(diagnostic_log_file_name="d.jsonl")
        self._bypass_validator(ctrl.config, "diagnostic_log_file_name", "/tmp/escaped.jsonl")
        with self.assertRaises(ValueError) as ctx:
            ctrl.diagnostic_log_path
        self.assertIn("diagnostic_log_file_name", str(ctx.exception))

    def test_diagnostic_log_path_valid_name_still_composes_under_data(self):
        ctrl = self._controller(diagnostic_log_file_name="d.jsonl")
        self.assertEqual(Path("data"), ctrl.diagnostic_log_path.parent)

    def test_state_path_abs_stays_within_data(self):
        """state_path_abs resolves through state_path, so the assertion covers it too."""
        ctrl = self._controller(state_file_name="x.json")
        self.assertEqual(Path("data").resolve() / "x.json", ctrl.state_path_abs)

    def test_opt_out_absolute_path_is_permitted_at_runtime(self):
        """When the operator opted out, the absolute path is the intended destination and the
        runtime check must NOT block it."""
        target = Path(self._tmp.name) / "opted_out.json"
        ctrl = self._controller(state_file_name=str(target), allow_absolute_state_file_name=True)
        self.assertEqual(target, ctrl.state_path)

    def test_traversal_via_the_id_derived_default_name_raises(self):
        """When state_file_name is unset the name is interpolated from config.id
        (`range_inventory_ladder_{id}.json`). CONTRACT C2 constrains `id` to a non-empty
        stripped str -- it does NOT make it path-safe -- so a traversing id reaches the same
        composition. The runtime assertion is the only thing standing in front of it."""
        ctrl = self._controller(state_file_name=None, id="../../evil")
        with self.assertRaises(ValueError) as ctx:
            ctrl.state_path
        # Assert the ValueError is OUR containment refusal, not an incidental one.
        self.assertIn("state_file_name", str(ctx.exception))

    def test_opt_out_does_not_permit_escaping_that_absolute_directory(self):
        """The opt-out authorizes ONE directory, not the whole filesystem: a traversal planted
        past the opted-out absolute path must still raise."""
        target = Path(self._tmp.name) / "sub" / "opted_out.json"
        ctrl = self._controller(state_file_name=str(target), allow_absolute_state_file_name=True)
        self._bypass_validator(
            ctrl.config, "state_file_name", str(Path(self._tmp.name) / "sub" / ".." / ".." / "escaped.json")
        )
        with self.assertRaises(ValueError):
            ctrl.state_path


if __name__ == "__main__":
    unittest.main()
