# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project

ossie-clickhouse is an open-source implementation of the
[Apache Ossie](https://github.com/apache/ossie) semantic model standard for
ClickHouse. It reads an Ossie data description, translates metric definitions
into ClickHouse SQL, answers questions posed in business terms (metrics,
dimensions, filters), and serves the description and queries to AI agents over
MCP. See [README.md](README.md) for the full product description.

Licensed under Apache 2.0.

## Language

All text in this repository is in English only: code identifiers, comments,
docstrings, commit messages, documentation, issues, and test names. Do not
introduce text in any other language, even in examples or fixtures.

## Scope

In scope:

- Reading and validating Ossie descriptions.
- Translating Ossie expressions into ClickHouse SQL, including ClickHouse
  specifics the user must not have to think about: deduplication of tables that
  keep change history, reference data stored in dictionaries, join resolution.
- Executing queries against any ClickHouse (self-hosted or cloud).
- Serving the model and accepting questions over MCP, with access controlled
  by a local configuration.

Out of scope, do not build:

- Chat, charts, or any user interface.
- Data loading, ETL, or warehouse features. Data is already in ClickHouse.
- Dependencies beyond ClickHouse and the Ossie standard where avoidable.

## Design principles

- The Ossie description is the single source of truth. Never hardcode metric
  or table semantics that belong in the model.
- Follow the standard's semantics exactly. When ClickHouse behaviour differs
  from the standard's assumptions, handle it in the translator, not by
  changing what the standard means.
- Every answer must be reproducible: the same question and the same model
  yield the same SQL.
- Keep the public surface small. Prefer one obvious way to do each thing.

## Stack

Python library with a CLI entry point. The parts that constrain code:

- **SQLGlot** for expression translation, parsing with its default dialect
  until the upstream Ossie dialect (apache/ossie PR #222) lands. Models today
  carry `ANSI_SQL` expressions only; prefer `OSSIE_SQL_2026` when present.
- **clickhouse-connect** over HTTP for execution.
- **MCP SDK** only behind the `[mcp]` extra. The core library must import
  without it.
- **apache-ossie** package (from the Ossie repository, git dependency until
  published) for loading and validating models. Do not write a model loader.
- Supported Ossie schema version is pinned (`0.2.0.dev0`) and checked
  explicitly.

## Architecture rules

- Layering: model loader -> translator -> planner -> executor -> MCP adapter.
  Lower layers never import higher ones.
- The MCP server (`mcp_server.py`, `[mcp]` extra) is a thin adapter over
  the library. No logic lives only in the MCP layer; each tool is one
  library call, and errors go back as results with an `error` field, never
  as exceptions, so suggestions reach the agent. Keep `client_factory` as
  the only place that decides which ClickHouse connection a caller gets.
- Access control is ClickHouse's. Queries run as the connected user; the
  model is trimmed (`access.py`) to the sources and columns that user can
  read, as revealed by `system.tables` and `system.columns`. A hidden object
  must never appear in SQL, errors or suggestions. The optional policy file
  only hides more; it never grants. Nothing in this project authenticates.
  A field or metric whose expression does not translate is hidden the same
  way (nothing proves it reads only visible columns) and reported by
  `validate`.
- ClickHouse specifics come from introspecting `system.tables`,
  `system.columns` and `system.dictionaries`, not from asking the user.
  `Replacing*` engines are read with `FINAL` (measured faster than `argMax`);
  single-key dictionaries with `dictGetOrNull` instead of a join. Overrides
  go in the model's `custom_extensions` under `vendor_name: CLICKHOUSE` as
  JSON, currently `{"dedup": "none"}`.
- The planner never talks to the database; it takes an optional `Catalog`
  the executor built. Keep that boundary.
- Query planning is deterministic: datasets from `source`, joins from
  `relationships`, one `SELECT` per question. Joins are `LEFT JOIN` from
  one root dataset, only along relationships whose `to_columns` are a
  primary or unique key of the target. Generated SQL carries
  `SETTINGS join_use_nulls = 1` so unmatched rows get NULL, not defaults.
- Name resolution is case-insensitive (spec rule); emitted SQL uses physical
  names exactly as the model writes them (ClickHouse is case-sensitive).
- Planner errors name the nearest known object; agents recover from that.

## Testing

Translation tests compare values, not only that ClickHouse accepts the SQL:
a translation that runs and returns the wrong value is the failure mode to
guard against. Expected values come from DuckDB by default, as a second
opinion close to the spec's Postgres-like semantics, not as ground truth.
Where DuckDB lacks a function or disagrees with the spec, the expected value
is written by hand in the test with a reference to the spec section. The
spec text wins over any engine. Replace hand-written expectations with the
Ossie compliance suite (apache/ossie PR #237) once it exists. Integration tests run
against the TPC-DS reference model from the Ossie repository, loaded into a
local ClickHouse. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup.

## Known ClickHouse traits to handle

- Float division by zero and single-row `STDDEV`/`VARIANCE` yield `inf` or
  `nan`, not `NULL`; the executor maps both to `None`.
- No `TO_DATE`, `SPLIT_PART`, `CONTAINS`, `IFF`, `ZEROIFNULL` and similar;
  each has a ClickHouse equivalent in the mapping table.
- `EXTRACT(DAYOFWEEK | DAYOFYEAR ...)` is not accepted; use `toDayOfWeek`
  and `toDayOfYear`.
- Table names are `database.table`; Ossie `source` may have three parts.
- `REGEXP_REPLACE` replaces all matches (spec reading); `DAYOFWEEK` is ISO.
- NULLs sort last in both directions, ClickHouse's default (the spec is
  silent): `NULL_ORDERING` on the parser dialect, `nulls_first=False` in
  the planner. Keep it when parsing switches to the Ossie dialect.
- `PERCENTILE_DISC` is an index into a sorted `groupArray`, not
  `quantileExact`, which picks one element too high whenever `p * n` is
  whole (Postgres semantics: the first value whose cumulative share
  reaches `p`).
- Function rewrites live in `translate.py` and work on the SQLGlot AST,
  never on SQL text.
- A rewritten function gets its spec signature in `_SIGNATURES`; `parse()`
  rejects other argument counts, so a rewrite never drops an argument.
- `clickhouse-connect` does not decode the `Time` type.

## Development

Python 3.11+, managed with uv. Layout: `src/ossie_clickhouse/` (library and
CLI), `tests/` (pytest, fixtures in `tests/fixtures/`), `docs/`, `research/`
(studies behind decisions and data for Apache Ossie; never imported by the
library or tests, linted with the rest).

```bash
uv sync                       # install with dev tools
uv run pytest                 # tests; ClickHouse-dependent ones skip when no server
uv run pytest -m "not integration"   # unit tests only
uv run ruff check src tests research   # lint
uv run ruff format src tests research  # format
uv run ossie-clickhouse validate tests/fixtures/tpcds.yaml [--url http://127.0.0.1:8123]
uv run ossie-clickhouse sql tests/fixtures/tpcds.yaml -m total_sales -d item.i_brand
uv run ossie-clickhouse query tests/fixtures/tpcds.yaml -m total_sales -d item.i_brand --json
uv run ossie-clickhouse query model.yaml -m revenue --url http://analyst:secret@host:8123 --policy policy.yaml
uv run ossie-clickhouse serve tests/fixtures/tpcds.yaml   # MCP over stdio
```

Access-control tests create users and need SQL access management on the
server (`access_management` for the connecting user; in the official Docker
image `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`). They skip otherwise.

Tests that use the `clickhouse` fixture are integration tests: they need a
server at `OSSIE_CLICKHOUSE_URL` (default `http://127.0.0.1:8123`) and skip
otherwise. DuckDB supplies default expected values for comparisons and needs
no server. Every change to library code comes with a test. Keep modules flat; add a
package level only when a module outgrows one file.

## Repository status

Version 0.1.0 is the first release (see [CHANGELOG.md](CHANGELOG.md)); installed from git until `apache-ossie` reaches PyPI. Decisions with their evidence are in [docs/design.md](docs/design.md).
Pull requests are merged with rebase; releases follow
[CONTRIBUTING.md](CONTRIBUTING.md#releasing).
