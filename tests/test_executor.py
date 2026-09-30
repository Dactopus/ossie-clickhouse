"""Executor against a ClickHouse test database with a ReplacingMergeTree fact
table and a dictionary dimension, created here."""

from pathlib import Path

import pytest

from ossie_clickhouse import load_model
from ossie_clickhouse.cli import main
from ossie_clickhouse.executor import Executor, overrides
from ossie_clickhouse.planner import Query

FIXTURE = Path(__file__).parent / "fixtures" / "history.yaml"
SETUP = """
CREATE DATABASE IF NOT EXISTS ossie_test;
DROP DICTIONARY IF EXISTS ossie_test.country;
DROP TABLE IF EXISTS ossie_test.orders;
DROP TABLE IF EXISTS ossie_test.country_src;
CREATE TABLE ossie_test.orders (order_id UInt32, amount Float64, country_code String, ver UInt32)
  ENGINE = ReplacingMergeTree(ver) ORDER BY order_id;
INSERT INTO ossie_test.orders VALUES (1, 10, 'DE', 1), (2, 20, 'FR', 1), (3, 30, 'XX', 1);
INSERT INTO ossie_test.orders VALUES (1, 15, 'DE', 2);
CREATE TABLE ossie_test.country_src (code String, name String) ENGINE = Memory;
INSERT INTO ossie_test.country_src VALUES ('DE', 'Germany'), ('FR', 'France');
CREATE DICTIONARY ossie_test.country (code String, name String) PRIMARY KEY code
  SOURCE(CLICKHOUSE(TABLE 'country_src' DB 'ossie_test')) LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(0);
"""


@pytest.fixture(scope="module")
def ex(clickhouse):
    for stmt in filter(None, (s.strip() for s in SETUP.split(";"))):
        clickhouse.command(stmt)
    yield Executor(clickhouse, load_model(FIXTURE))
    clickhouse.command("DROP DATABASE ossie_test")


def test_introspection(ex):
    assert ex.catalog["orders"].engine == "ReplacingMergeTree" and ex.catalog["orders"].dedup
    assert not ex.catalog["orders_raw"].dedup  # overridden
    assert ex.catalog["country"].engine == "Dictionary"
    assert ex.catalog["country"].dictionary_key == "code"
    assert overrides(ex.model.datasets[1]) == {"dedup": "none"}


def test_dedup_with_final(ex):
    r = ex.execute(Query(metrics=("revenue",)))
    assert "FROM ossie_test.orders AS orders FINAL" in r.sql
    assert r.rows == [(65.0,)]  # 15 + 20 + 30, not 10 + 15 + 20 + 30
    raw = ex.execute(Query(metrics=("raw_revenue",)))
    assert "FINAL" not in raw.sql and raw.rows == [(75.0,)]


def test_dictionary_read_with_dictget(ex):
    r = ex.execute(Query(metrics=("revenue",), dimensions=("country.name",)))
    assert "dictGetOrNull('ossie_test.country', 'name', orders.country_code)" in r.sql
    assert "JOIN" not in r.sql
    assert set(r.rows) == {(None, 30.0), ("France", 20.0), ("Germany", 15.0)}


def test_dictionary_key_column_is_the_join_key(ex):
    # dictGet* cannot read the key; unknown keys are NULL as with a LEFT JOIN.
    r = ex.execute(Query(metrics=("revenue",), dimensions=("country.code",)))
    assert "dictGetOrNull('ossie_test.country', 'code'" not in r.sql
    assert set(r.rows) == {(None, 30.0), ("FR", 20.0), ("DE", 15.0)}


def test_nan_and_inf_become_none(ex):
    r = ex.execute(Query(metrics=("spread",), dimensions=("orders.order_id",), limit=1))
    assert r.rows[0][1] is None
    assert ex.execute(Query(metrics=("zero_ratio",))).rows == [(None,)]


def test_odd_source_names_do_not_break_introspection(ex, tmp_path):
    p = tmp_path / "quoted.yaml"
    text = FIXTURE.read_text().replace(
        "source: ossie_test.country\n", "source: ossie_test.o'brien\\x\n"
    )
    p.write_text(text)
    problems = Executor(ex.client, load_model(p)).check()
    assert any('source "ossie_test.o\'brien\\\\x" not found' in x for x in problems)


def test_check_reports_database_level_problems(ex, tmp_path):
    assert ex.check() == []
    text = FIXTURE.read_text().replace("expression: amount}", "expression: amnt}", 1)
    text = text.replace("source: ossie_test.country", "source: ossie_test.countries")
    p = tmp_path / "bad.yaml"
    p.write_text(text)
    problems = Executor(ex.client, load_model(p)).check()
    assert any("column 'amnt' (field 'amount') not in ossie_test.orders" in x for x in problems)
    assert any("source 'ossie_test.countries' not found" in x for x in problems)
    assert main(["validate", str(p), "--url", ex.client.url]) == 1


def test_cli_query(ex, capsys):
    assert (
        main(
            [
                "query",
                str(FIXTURE),
                "-m",
                "revenue",
                "-d",
                "country.name",
                "--url",
                ex.client.url,
                "--json",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert '"revenue": 15.0' in out and '"name": null' in out
    assert main(["query", str(FIXTURE), "-m", "revenue", "--url", ex.client.url]) == 0
    assert capsys.readouterr().out == "revenue\n65.0\n"
    assert main(["sql", str(FIXTURE), "-m", "revenue", "--url", ex.client.url]) == 0
    assert "FINAL" in capsys.readouterr().out
