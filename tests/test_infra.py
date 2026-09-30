"""Smoke tests for the test fixtures themselves."""


def test_duckdb_reference(duck):
    assert duck.execute("SELECT 1 + 1").fetchone()[0] == 2


def test_clickhouse_reachable(clickhouse):
    assert clickhouse.query("SELECT 1 + 1").result_rows[0][0] == 2
