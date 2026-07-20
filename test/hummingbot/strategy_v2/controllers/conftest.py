"""Directory-wide fixtures for the strategy_v2 controller tests.

CDX-011 / CLA-305: controllers now own a persisted TradeLedger that defaults to
writing under the bot's data/ directory. Left unpatched, any test that drives a
wired controller past a fill would write into the repo's data/ dir and — worse —
leak fill history ACROSS tests and ACROSS pytest runs (colliding controller ids
would reload each other's fills and silently flip cooldown/cap gates).

The autouse fixture below points the module-level default ledger directory at a
per-test temporary directory. Tests that exercise real persistence construct
TradeLedger with an explicit base_dir, which bypasses this default entirely.
"""
import pytest

from controllers._shared import trade_ledger


@pytest.fixture(autouse=True)
def _isolated_trade_ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_ledger, "DEFAULT_LEDGER_DIR", tmp_path / "trade_ledgers")
    yield
