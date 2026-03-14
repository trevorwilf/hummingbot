"""
NonKYC connector test configuration.

test_nonkyc_live_api.py is marked as live_api because it hits real endpoints
and uses custom result() counters instead of pytest assertions.
It is excluded from the default CI lane.
"""
import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-mark test_nonkyc_live_api tests as live_api."""
    for item in items:
        if "test_nonkyc_live_api" in item.nodeid:
            item.add_marker(pytest.mark.live_api)
