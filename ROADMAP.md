# Roadmap

Intended order of work. Phases are sequential unless noted; each one ends
with something runnable. No dates: the order is a commitment, the pace is not.

## Phase 0: Feasibility spike (done)

Completed 2026-09-30. Findings in [docs/phase-0-findings.md](docs/phase-0-findings.md).
The SQLGlot-based translation design holds. Later phases below were adjusted
from the findings.

## Phase 1: Model loader (done)

Completed 2026-09-30. `load_model()` builds on the `apache-ossie` package
(git dependency until it is on PyPI), pins the schema version, and adds the
checks upstream lacks: unique names, relationships that resolve, expressions
in a translatable dialect. CLI: `ossie-clickhouse validate <model>`.
Key and relationship columns are physical column names per the spec, so
their existence is checked against the database in Phase 4, not here.

## Phase 2: Expression translation (done)

Completed 2026-09-30. `translate.py` parses with SQLGlot's default dialect
plus the spec's argument order for `DATEADD`, `DATEDIFF` and `DATE_PART`,
rewrites the spec functions ClickHouse lacks on the AST, and generates with
SQLGlot's ClickHouse dialect. `OSSIE_SQL_2026` is preferred, `ANSI_SQL` is
the fallback and what models carry today.

Tests: every spec catalog function executed in ClickHouse and compared by
value with DuckDB, or with a hand-pinned expectation where DuckDB lacks the
function or disagrees with the spec; every TPC-DS field and metric executed
against loaded data. Decisions recorded in the tests: `REGEXP_REPLACE`
replaces all matches, `DAYOFWEEK` is ISO (Monday = 1), `MILLISECOND` is the
component. PR #222 evaluation in
[docs/phase-2-pr222-evaluation.md](docs/phase-2-pr222-evaluation.md).

Left open, on purpose:

- NaN versus NULL (`STDDEV` over one row, float division by zero) is an
  executor concern: convert in the result set in Phase 4, not in SQL.
- Switch parsing to the upstream dialect once apache/ossie PR #222 merges.
- No validation of disallowed constructs (subqueries, `SELECT`) yet; add
  when the planner needs to trust expressions, or take it from PR #222.

## Phase 3: Query planner, minimum

- Semantic query API: metrics, dimensions, filters. Filters are Ossie
  expressions, translated like any other.
- Reference resolution: walk each expression's AST, find `dataset.field`
  references, resolve them against the model case-insensitively (the spec
  normalizes unquoted identifiers to upper case, ClickHouse is
  case-sensitive), inline field expressions, and emit physical names in
  their real case.
- Mapping rule from Ossie `source` names (three-part, e.g.
  `tpcds.public.store_sales`) to ClickHouse `database.table`. Table and view
  sources only; `source` as a query is deferred.
- Queries over a single dataset, then joins along direct `relationships`
  between two datasets. A join is allowed only when `to_columns` match the
  primary key or a unique key of the target dataset, which is what makes it
  many-to-one and safe from fan-out. The spec has no cardinality attribute,
  so this is inferred, and anything else is rejected with a clear message.
- Window metrics over aggregates (running totals, ranks, period deltas, as
  in the TPC-DS reference model) pass through in the same `SELECT`. The
  spec has no way to declare a metric's required grain, so the planner
  cannot check that the right dimensions were requested; note as a spec gap.
- Validation of disallowed constructs in expressions (subqueries, `SELECT`),
  since the planner now has to trust them.
- One deterministic `SELECT` per question. Snapshot tests on generated SQL,
  plus execution against TPC-DS.
- CLI: `ossie-clickhouse sql <model> --metric ... --dimension ... --filter ...`.
- CI: GitHub Actions running the suite with a ClickHouse service container.

Explicitly deferred to Phase 8: multi-fact queries, joins that are not
many-to-one, metrics over metrics.

## Phase 4: Executor

- Execution through `clickhouse-connect` against self-hosted and Cloud.
- Verify the model against the database: sources exist, key and
  relationship columns exist.
- Introspection: engine per table from `system.tables`, dictionaries from
  `system.dictionaries`.
- Deduplication for `ReplacingMergeTree`. Evaluate `FINAL` against
  `argMax` on the version column and pick the default on measured cost.
- `dictGet` for reference data held in dictionaries.
- Overrides via `custom_extensions` with `vendor_name: CLICKHOUSE`
  (`vendor_name` is a free string in the spec; upper case by convention).
- NaN to NULL in result sets (ClickHouse yields `nan` where SQL yields
  `NULL`).
- CLI: `ossie-clickhouse query ...`.

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
- Switch `apache-ossie` from the git pin to the PyPI release once published.
- Switch expression parsing to the upstream dialect once apache/ossie
  PR #222 merges; adopt the compliance suite (PR #237) when it lands.
- Contribute ClickHouse-specific findings upstream where the standard is
  silent.
