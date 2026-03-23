"""
MEXC connector test configuration.

test_mexc_live_api.py is marked as live_api because it hits real endpoints.
It is excluded from the default CI lane (run with -m live_api to include).
"""
import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-mark test_mexc_live_api tests as live_api."""
    for item in items:
        if "test_mexc_live_api" in item.nodeid:
            item.add_marker(pytest.mark.live_api)
