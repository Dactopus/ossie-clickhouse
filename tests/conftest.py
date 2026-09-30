"""Shared fixtures.

Unit tests run anywhere. Tests marked ``integration`` need a ClickHouse
reachable over HTTP and skip cleanly when there is none. Set
``OSSIE_CLICKHOUSE_URL`` (default ``http://127.0.0.1:8123``) to point
elsewhere.
"""

import os

import pytest


@pytest.fixture(scope="session")
def duck():
    """In-memory DuckDB connection: default expected values, not ground truth."""
    import duckdb

    con = duckdb.connect()
    yield con
    con.close()


@pytest.fixture(scope="session")
def clickhouse():
    """clickhouse-connect client, or skip the test when no server answers."""
    import clickhouse_connect

    url = os.environ.get("OSSIE_CLICKHOUSE_URL", "http://127.0.0.1:8123")
    try:
        client = clickhouse_connect.get_client(dsn=url, connect_timeout=2)
        client.command("SELECT 1")
    except Exception as e:
        pytest.skip(f"no ClickHouse at {url}: {e}")
    yield client
    client.close()


def pytest_collection_modifyitems(items):
    """Every test using the ``clickhouse`` fixture is an integration test."""
    for item in items:
        if "clickhouse" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)
