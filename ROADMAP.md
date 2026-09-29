# Roadmap

Intended order of work. Phases are sequential unless noted; each one ends
with something runnable. No dates: the order is a commitment, the pace is not.

## Phase 0: Feasibility spike (done)

Completed 2026-09-30. Findings in [docs/phase-0-findings.md](docs/phase-0-findings.md).
The SQLGlot-based translation design holds. Later phases below were adjusted
from the findings.

## Phase 1: Model loader

- Build on the `apache-ossie` package from the Ossie repository (Pydantic
  models, YAML and JSON loading). Pin it as a git dependency until it is on
  PyPI.
- Pin the supported schema version (`0.2.0.dev0` today) and reject others
  with a clear message.
- CLI: `ossie-clickhouse validate <model>`.

## Phase 2: Expression translation

- Translate field and metric expressions to ClickHouse SQL.
- Function mapping table for the gaps found in Phase 0 (about 25 entries),
  a `TO_CHAR` format token table, and a NaN-versus-NULL policy.
- `ANSI_SQL` is what models carry today; treat it as the primary input and
  `OSSIE_SQL_2026` as preferred when present.
- Tests compare values against a reference engine (DuckDB), not only that
  ClickHouse accepts the SQL. Cover every expression in the TPC-DS model and
  every function in the spec catalog.
- Switch to the upstream SQLGlot Ossie dialect once apache/ossie PR #222
  lands.

## Phase 3: Query planner, minimum

- Semantic query API: metrics, dimensions, filters.
- Queries over a single dataset.
- Joins along direct `relationships` between two datasets.
- One deterministic `SELECT` per question. Snapshot tests on generated SQL.
- CLI: `ossie-clickhouse sql <model> --metric ... --dimension ...`.

Explicitly deferred to Phase 7: multi-fact queries, fan-out protection,
metrics over metrics.

## Phase 4: Executor

- Execution through `clickhouse-connect` against self-hosted and Cloud.
- Mapping rule from Ossie `source` names (three-part, e.g.
  `tpcds.public.store_sales`) to ClickHouse `database.table`.
- Introspection: engine per table from `system.tables`, dictionaries from
  `system.dictionaries`.
- Deduplication for `ReplacingMergeTree`. Evaluate `FINAL` against
  `argMax` on the version column and pick the default on measured cost.
- `dictGet` for reference data held in dictionaries.
- Overrides via `custom_extensions` with `vendor_name: clickhouse`.
- CLI: `ossie-clickhouse query ...`.
- Integration tests against local ClickHouse in CI.

## Phase 5: Access control

- Local configuration mapping roles to visible datasets, fields and metrics.
- Enforced in the executor, before planning touches anything the role may
  not see.
- Tests that a denied object never appears in generated SQL or in errors.

## Phase 6: MCP server

- Optional extra `ossie-clickhouse[mcp]`; the core library imports without
  the MCP SDK.
- Tools: list model, describe object with `ai_context`, run semantic query.
- Caller identity passed through to access control, nothing else.
- Thin adapter only: no logic that does not exist in the library API.

## Phase 7: First public release

- Packaging and publication to PyPI.
- Documentation: installation, model authoring for ClickHouse, CLI, MCP
  setup, access configuration.
- Contribution guide.
- Repository made public.

## Phase 8: Query planner, full

Open-ended. Scope decided from user feedback after the first release.

- Multi-fact queries with conformed dimensions.
- Fan-out protection for one-to-many and many-to-many joins.
- Metrics defined over other metrics.
- Derived dimensions and time grain handling.

## Ongoing

- Track Ossie schema changes; bump the pinned version deliberately, never
  implicitly.
- Contribute ClickHouse-specific findings upstream where the standard is
  silent.
