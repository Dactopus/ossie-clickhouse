# Contributing

Bug reports, questions about a translation, and pull requests are welcome.
Everything in this repository is in English: code, comments, commit
messages, issues.

## Reporting a wrong answer

The failure mode this project guards against is SQL that runs and returns
the wrong value. If you see one, open an issue with the model (or the part
of it involved), the question (metrics, dimensions, filters), the SQL from
`ossie-clickhouse sql`, and what the value should be, with the spec section
or the hand-written query that says so.

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Dactopus/ossie-clickhouse
cd ossie-clickhouse
uv sync
uv run pytest -m "not integration"     # unit tests, no server needed
```

Integration tests need a ClickHouse at `OSSIE_CLICKHOUSE_URL` (default
`http://127.0.0.1:8123`) and skip without one. Access-control tests also
need SQL access management for the connecting user:

```bash
docker run -d --name ch -p 8123:8123 -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 clickhouse/clickhouse-server
uv run pytest
```

Tests against the TPC-DS reference model need the `tpcds` database with
the five tables the model uses, and skip when it is absent (CI runs
without it). DuckDB, already a dev dependency, generates the data at
scale factor 1:

```bash
mkdir -p /tmp/tpcds && uv run python -c "
import duckdb; c = duckdb.connect(); c.execute('INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf=1)')
for t in ['store_sales', 'date_dim', 'customer', 'item', 'store']:
    c.execute(f\"COPY {t} TO '/tmp/tpcds/{t}.parquet' (FORMAT PARQUET)\")"
docker cp /tmp/tpcds/. ch:/var/lib/clickhouse/user_files/
docker exec ch clickhouse-client -q "CREATE DATABASE IF NOT EXISTS tpcds"
for t in store_sales date_dim customer item store; do
  docker exec ch clickhouse-client -q "CREATE TABLE tpcds.$t ENGINE = MergeTree ORDER BY tuple() AS SELECT * FROM file('$t.parquet', Parquet)"
done
```

```bash
uv run ruff check src tests
uv run ruff format src tests
```

## Rules that shape the code

[AGENTS.md](AGENTS.md) is the full list; the ones that matter most for a
pull request:

- The Ossie description is the single source of truth. Semantics that
  belong in the model are never hardcoded.
- Follow the standard exactly. When ClickHouse differs from the spec,
  handle it in the translator; do not change what the standard means.
- Every change to library code comes with a test. Translation tests
  compare values, not only that ClickHouse accepts the SQL: DuckDB gives
  the default expected value, a hand-written expectation with a spec
  reference where DuckDB lacks the function or disagrees, and the spec
  text wins over any engine.
- Layering: model loader, translator, planner, executor, MCP adapter.
  Lower layers never import higher ones. The planner never talks to the
  database. The MCP server holds no logic of its own.
- Access control is ClickHouse's. Nothing here authenticates or grants.
- Keep the public surface small. One obvious way to do each thing.

## Layout

| Path | What |
| --- | --- |
| `src/ossie_clickhouse/model.py` | Load and validate a model on top of `apache-ossie`. |
| `src/ossie_clickhouse/translate.py` | Ossie expression to ClickHouse SQL, on the SQLGlot AST. |
| `src/ossie_clickhouse/planner.py` | Question to one `SELECT`. |
| `src/ossie_clickhouse/executor.py` | Introspection, execution, result cleanup. |
| `src/ossie_clickhouse/access.py` | Trim the model to the caller's rights; policy file. |
| `src/ossie_clickhouse/mcp_server.py` | MCP tools, behind the `[mcp]` extra. |
| `src/ossie_clickhouse/cli.py` | `ossie-clickhouse` command. |
| `tests/` | pytest; fixtures in `tests/fixtures/`. |

## Pull requests

Small and focused. Say which spec section a semantic decision rests on.
CI runs lint, format check and the test suite against a ClickHouse
service. By submitting a contribution you agree it is licensed under the
Apache License 2.0, like the rest of the project.
