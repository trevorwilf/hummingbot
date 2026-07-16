"""
CONTRACT C2 — controller `id` contract (CDX-008 / CLA-002), engine half.

ACCEPT: a `str` whose stripped length is >= 1. Canonical id = the stripped value; all identity
derivations (ledger filename, `.owner` match) use the canonical id.
REJECT: non-`str` (int, bool, None), `""`, whitespace-only.

Every expectation below is derived from that contract, not from running the implementation.
"""
import unittest

from annotated_types import MinLen
from pydantic import ValidationError

from hummingbot.strategy_v2.controllers.controller_base import ControllerConfigBase


class ChildControllerConfig(ControllerConfigBase):
    """Proves the contract is inherited by real controller configs, not just enforced on the base."""
    controller_name: str = "child_controller"


def build(id_value) -> ControllerConfigBase:
    return ControllerConfigBase(id=id_value, controller_name="test_controller")


class TestControllerIdContractRejects(unittest.TestCase):
    """REJECT set — each case must fail closed at config validation."""

    def test_empty_string_rejected(self):
        with self.assertRaises(ValidationError):
            build("")

    def test_whitespace_only_rejected(self):
        with self.assertRaises(ValidationError):
            build("   ")

    def test_tab_newline_only_rejected(self):
        with self.assertRaises(ValidationError):
            build("\t\n")

    def test_int_zero_rejected(self):
        with self.assertRaises(ValidationError):
            build(0)

    def test_bool_false_rejected(self):
        with self.assertRaises(ValidationError):
            build(False)

    def test_int_rejected(self):
        with self.assertRaises(ValidationError):
            build(123)

    def test_none_rejected(self):
        with self.assertRaises(ValidationError):
            build(None)

    def test_missing_id_rejected(self):
        with self.assertRaises(ValidationError):
            ControllerConfigBase(controller_name="test_controller")

    def test_rejection_is_inherited_by_subclass(self):
        with self.assertRaises(ValidationError):
            ChildControllerConfig(id="   ")


class TestControllerIdContractAccepts(unittest.TestCase):
    """ACCEPT set — stripped length >= 1; stored value is the canonical (stripped) id."""

    def test_plain_id_accepted_unchanged(self):
        self.assertEqual("abc", build("abc").id)

    def test_padded_id_accepted_and_canonicalized(self):
        self.assertEqual("abc", build(" abc ").id)

    def test_internal_whitespace_preserved(self):
        # Only the ends are stripped: the canonical id is `v.strip()`, nothing more.
        self.assertEqual("a b", build("  a b  ").id)

    def test_canonicalization_is_inherited_by_subclass(self):
        self.assertEqual("abc", ChildControllerConfig(id="\tabc\n").id)


class TestControllerIdContractEnforcement(unittest.TestCase):
    """The contract must hold everywhere the id can enter the model, and be declared in the schema."""

    def test_assignment_of_whitespace_id_rejected(self):
        # BaseClientModel sets validate_assignment=True; identity derivations read config.id
        # after construction, so a later assignment must not be able to install a blank id.
        config = build("abc")
        with self.assertRaises(ValidationError):
            config.id = "   "
        self.assertEqual("abc", config.id)

    def test_assignment_is_canonicalized(self):
        config = build("abc")
        config.id = " xyz "
        self.assertEqual("xyz", config.id)

    def test_min_length_constraint_is_declared_on_the_field(self):
        # C2 requires the constraint on the field itself, not only in the validator: it is what
        # propagates into the JSON schema that clients validate against before ever reaching
        # the validator. The strip validator makes it behaviourally redundant, so this
        # structural assertion is the only thing that catches its removal.
        self.assertIn(MinLen(1), ControllerConfigBase.model_fields["id"].metadata)

    def test_error_message_names_the_violated_contract(self):
        with self.assertRaises(ValidationError) as ctx:
            build("   ")
        self.assertIn("empty or whitespace-only", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
