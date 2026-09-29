# Phase 0 findings: translation feasibility

Date: 2026-09-30. ClickHouse 26.9.5, SQLGlot 30.20, apache/ossie main at
adfa9e4 (2026-09-29). Scripts and raw output in [spikes/phase0](../spikes/phase0).

## Decision

The SQLGlot-based translation design holds. Proceed to Phase 1 with the
adjustments listed under "Consequences for the roadmap".

## What was measured

Every expression was translated by SQLGlot (parsed with the default dialect,
generated for ClickHouse) and then executed against a local ClickHouse loaded
with TPC-DS scale factor 1. The spec function catalog was additionally
evaluated in DuckDB as a reference to catch translations that run but return
a different value.

| Set | Translated and executed | Notes |
|---|---|---|
| TPC-DS model fields (31) | 31 / 31 | All are bare column references except one `\|\|` concatenation |
| TPC-DS model metrics (8) | 8 / 8 | Includes three window-over-aggregate metrics |
| Spec function catalog (143 samples) | 122 / 143 | 21 failures, all missing function mappings |
| Catalog value check vs DuckDB (105 comparable) | 94 equal | 11 differ; see below |

Zero parse failures. SQLGlot's default dialect accepts everything the spec
lists as supported, including `WITHIN GROUP`, typed literals and window frames.

## Failures and how they resolve

All 21 catalog failures are one-line entries in a function mapping table.
Grouped by cause:

- **SQLGlot emits a name ClickHouse lacks**: `VAR_POP` -> `VARIANCE_POP`
  (ClickHouse: `varPop`), `DAYOFYEAR` -> `DAY_OF_YEAR` (`toDayOfYear`),
  `DATEDIFF(day, d, d2)` -> `DATE_DIFF(D2, d, day)` with the identifier
  upper-cased (SQLGlot bug, worth an upstream fix).
- **Spec function with no ClickHouse namesake**: `TO_DATE`, `TO_TIMESTAMP`,
  `SPLIT_PART`, `CONTAINS`, `REGEXP_COUNT`, `IFF`, `ZEROIFNULL`,
  `NULLIFZERO`, `APPROX_PERCENTILE`, `CURRENT_TIME`. ClickHouse has
  equivalents for each (`toDate`, `parseDateTimeBestEffort`, `splitByChar`,
  `position > 0`, `countMatches`, `if`, `ifNull(x, 0)`, `nullIf(x, 0)`,
  `quantile`, `toTime`).
- **Syntax ClickHouse does not accept**: `PERCENTILE_CONT / PERCENTILE_DISC
  ... WITHIN GROUP` (map to `quantile` / `quantileExact`),
  `EXTRACT(DAYOFWEEK | DAYOFYEAR FROM d)` (map to `toDayOfWeek` /
  `toDayOfYear`).

## Silent mistranslations

More important than the failures. These translate, execute, and return the
wrong thing:

- `TO_CHAR(d, 'YYYY-MM-DD')` becomes `CAST(d AS String)`; SQLGlot drops the
  format with a warning. Needs `formatDateTime` plus a token conversion table
  from the spec's `YYYY/MM/DD/...` to ClickHouse `%Y/%m/%d/...`. The spec marks
  `TO_CHAR` experimental.
- `STDDEV`, `VARIANCE` over one row: ClickHouse returns `nan`, the reference
  returns `NULL`. NaN-versus-NULL is a general ClickHouse trait (division by
  zero on floats behaves the same way) and needs a policy, probably wrapping
  in `nanToNull`-style handling at the metric level.

Rule for Phase 2: the test suite must compare values against a reference
engine, not only check that ClickHouse accepts the SQL.

## Value differences that are not bugs

- `DATE_TRUNC` on a `Date` returns `Date` in ClickHouse, `TIMESTAMP` in
  DuckDB. Same instant, different type.
- `TIME` literals: ClickHouse handles them correctly; the zero came from the
  `clickhouse-connect` Python client, which does not decode the `Time` type.
  Track as a client limitation.
- `EXTRACT(MILLISECOND ...)`: ClickHouse returns the millisecond component,
  DuckDB returns seconds times 1000 plus milliseconds. The spec does not say
  which; ClickHouse's reading is the usual one.

## Other findings

- **The reference model uses only `ANSI_SQL`.** No `OSSIE_SQL_2026`
  expressions exist in any example or converter fixture in the Ossie
  repository. The fallback order in the README (prefer `OSSIE_SQL_2026`, fall
  back to `ANSI_SQL`) stays correct, but in practice `ANSI_SQL` is what
  models carry today.
- **`apache-ossie` Python package exists** in `python/` of the Ossie
  repository: Pydantic v2 models with YAML and JSON loading, Python 3.11+.
  Version `0.2.0.dev0`, not yet on PyPI. Phase 1 should build on it rather
  than write a loader.
- **Schema version** is a single `const` in the JSON schema, currently
  `0.2.0.dev0`. Pinning means matching that string.
- **Relationships in the reference model** are four many-to-one edges from
  one fact table to four dimensions, with no cardinality attribute. The
  planner minimum in Phase 3 covers this model fully.
- **`source` uses three-part names** (`tpcds.public.store_sales`).
  ClickHouse has `database.table` only. Phase 4 needs a mapping rule; the
  spike dropped the middle part.
- **Parquet from DuckDB loads every column as `Nullable`.** Real ClickHouse
  schemas will mostly be non-nullable; tests should cover both.
- **PR #222 (SQLGlot Ossie dialect)** is open, updated 2026-09-27, about
  1800 lines including a function table and validation. It was not
  exercised: installing code from an unreviewed fork branch is blocked by
  this session's policy. It is the natural upstream home for the function
  mapping table above, so tracking it stays worthwhile.

## Consequences for the roadmap

- Phase 1: depend on `apache-ossie` instead of writing a loader. Until it
  is on PyPI, pin it as a git dependency.
- Phase 2: add a reference-engine value comparison to the test plan. DuckDB
  is a reasonable reference for most of the catalog, with 24 spec functions
  it does not know.
- Phase 2: the function mapping table is about 25 entries, plus the `TO_CHAR`
  token table and a NaN policy.
- Phase 4: define the `source` name mapping rule.

## Environment notes

TPC-DS scale factor 1 generated by DuckDB's `tpcds` extension in 12 seconds;
the five tables the model uses take 176 MiB in ClickHouse. Every query in the
spike, including the five-way star join over 2.9 million rows, returned in
well under a second.
