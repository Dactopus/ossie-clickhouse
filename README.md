# ossie-clickhouse

An implementation of the [Apache Ossie](https://github.com/apache/ossie)
semantic model standard for ClickHouse. It reads an Ossie description of
your data, answers questions asked in business terms (metrics, dimensions,
filters) with one ClickHouse query, and serves the description and the
queries to AI agents over MCP.

## Why

A company connects an AI assistant to its database and the assistant gets
things wrong: hundreds of tables with no explanations, three columns with
the same name, a different "revenue" in every department. Apache Ossie
fixes this at the level of description: one open, portable definition of
what tables and fields mean, how they relate and how metrics are computed.
ossie-clickhouse executes those descriptions on ClickHouse, which the
standard does not cover itself, and handles the ClickHouse specifics
(tables that keep change history, reference data in dictionaries, join
resolution) so that neither the model author nor the agent has to.

## Install

Python 3.11 or later. Not on PyPI yet: the `apache-ossie` package this
builds on is a git dependency until upstream publishes it, and PyPI does
not accept packages with git dependencies.

```bash
pip install "ossie-clickhouse[mcp] @ git+https://github.com/Dactopus/ossie-clickhouse"
```

Leave out `[mcp]` to skip the MCP server and its SDK; the library and the
CLI work without them.

## Quick start

Point it at a model and a ClickHouse. The TPC-DS reference model from the
Ossie repository is in [tests/fixtures/tpcds.yaml](tests/fixtures/tpcds.yaml).
ClickHouse credentials go in the URL or in `OSSIE_CLICKHOUSE_URL`
(default `http://127.0.0.1:8123`).

```bash
ossie-clickhouse validate model.yaml --url http://user:password@host:8123
ossie-clickhouse sql model.yaml -m total_sales -d item.i_brand -f "date_dim.d_year = 1998"
ossie-clickhouse query model.yaml -m total_sales -d item.i_brand -f "total_sales > 1000000" --json
ossie-clickhouse serve model.yaml        # MCP over stdio, see docs/mcp-setup.md
```

From Python:

```python
from ossie_clickhouse import load_model
from ossie_clickhouse.executor import Executor, connect
from ossie_clickhouse.planner import Query

ex = Executor(connect("http://user:password@host:8123"), load_model("model.yaml"))
r = ex.execute(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))
print(r.sql, r.columns, r.rows[:3])
```

## How a question becomes SQL

- Datasets come from `source`, joins from `relationships`. One root
  dataset, `LEFT JOIN` to the dimensions the question touches, only along
  relationships whose `to_columns` are a primary or unique key of the
  target (many-to-one). `SETTINGS join_use_nulls = 1` so unmatched rows
  get NULL, not ClickHouse defaults.
- Filters over fields go to `WHERE`; filters that name a metric or contain
  an aggregate (`total_sales > 1000000`) go to `HAVING`.
- Tables with a `Replacing*` engine are read with `FINAL`; single-key
  dictionaries are read with `dictGetOrNull` instead of a join. Both are
  learned from `system.tables` and `system.dictionaries`, not configured.
- Expressions in `OSSIE_SQL_2026` or `ANSI_SQL` are translated on the
  SQLGlot AST; the spec's function catalog is mapped to ClickHouse
  equivalents and checked by value against DuckDB and the spec text.
- Names resolve case-insensitively; SQL uses the physical names as the
  model writes them. The same question and model always give the same SQL.
- Errors name the nearest known metric, field or dataset, so an agent can
  recover from a typo without help.

## CLI

| Command | What it does |
| --- | --- |
| `validate <model> [--url]` | Check the file; with `--url`, also that every source, column and relationship column exists in ClickHouse. |
| `sql <model> ...` | Print the SQL for a question without running it. |
| `query <model> ... [--json]` | Run it; TSV or JSON rows. |
| `serve <model>` | MCP server over stdio (needs the `[mcp]` extra). |

Question options for `sql` and `query`: `-m metric` (repeatable),
`-d dataset.field` (repeatable), `-f condition` (repeatable; a metric
name or an aggregate means `HAVING`), `-o "name [desc]"`, `-l limit`.
`--url` and `--policy` apply to anything that talks to ClickHouse.

## Documentation

- [Model authoring for ClickHouse](docs/model-authoring.md): sources,
  keys, expressions, deduplication, dictionaries, validation.
- [Access control](docs/access-control.md): ClickHouse users, roles and
  row policies decide; the policy file hides more.
- [MCP setup](docs/mcp-setup.md): Claude Desktop, Claude Code and other
  stdio clients.
- [Roadmap](ROADMAP.md) and [contributing](CONTRIBUTING.md).

## What it is not

No chat, no charts, no user interface: it provides access and
understanding, the interface is someone else's job. No data loading: the
data must already be in ClickHouse.

## Status

Pre-release. The supported Ossie schema version is pinned (`0.2.0.dev0`)
and checked on load, because the standard is still moving. Tested against
the TPC-DS reference model on ClickHouse 26.x.

## License

Apache License 2.0.
