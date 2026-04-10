import os

import pytest
from sqlalchemy import create_engine

PG_TEST_URL = os.environ.get("HBOT_TEST_PG_URL")


@pytest.fixture
def pg_engine():
    """PostgreSQL engine for integration tests. Skips if PG_TEST_URL not set."""
    if not PG_TEST_URL:
        pytest.skip("Set HBOT_TEST_PG_URL to run PostgreSQL integration tests")
    engine = create_engine(PG_TEST_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def sqlite_engine():
    """In-memory SQLite engine for fast unit tests."""
    engine = create_engine("sqlite:///:memory:")
    yield engine
    engine.dispose()
