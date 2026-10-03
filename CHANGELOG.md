# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). While the major version is 0,
a minor release may change the Python API.

## [Unreleased]

### Changed

- A relationship is joined when its `to_columns` cover a primary or unique
  key of the target, not only when they equal one, as the reference
  validator reads the spec (apache/ossie#330). A join on
  `(tenant_id, customer_id)` to customers keyed by `customer_id` used to
  fail with "not many-to-one". A target with no declared key gets its own
  error that says so.

- NULLs sort last in both directions, in window `ORDER BY` inside model
  expressions and in a query's `order_by`. Ascending orderings used to put
  NULLs first, a default inherited from SQLGlot rather than chosen; a
  running total over a key with NULLs, such as sales without a matching
  date, started from their sum. The spec does not define NULL ordering;
  this is ClickHouse's and DuckDB's default, so the generated SQL carries
  no `NULLS` clause.
- Expressions with `NULLS FIRST | LAST`, or with a window frame the spec
  does not list, are rejected; `validate` reports them. Accepted frames:
  `ROWS` frames that hold the current row, and `RANGE BETWEEN UNBOUNDED
  PRECEDING AND CURRENT ROW`. ClickHouse answered an empty frame with 0
  instead of NULL, and a `RANGE` offset counted NULL keys as within it.

### Fixed

- `LAG` and `LEAD` without a default, and `NTH_VALUE` past the end of the
  frame, return NULL as the spec (ANSI SQL) says, not 0. ClickHouse falls
  back to the type's default when the argument is not Nullable, so a
  month-over-month change on a non-Nullable column showed the first
  month's full value as its change. `FIRST_VALUE` and `LAST_VALUE`
  respect NULLs; ClickHouse skips them by default. `IGNORE NULLS` on
  `LAG`, `LEAD` and `NTH_VALUE` is rejected: ClickHouse ignored it.
- A rewritten function inside another rewritten one is translated too:
  `DAYOFYEAR(TO_DATE(s))` used to leave `TO_DATE` for ClickHouse, which
  has no such function.

## [0.1.1] - 2026-10-01

### Fixed

- Functions the translator rewrites are checked against the spec's
  signature. Longer forms from other engines, such as Snowflake's
  `REGEXP_COUNT(str, pattern, position, flags)`, used to lose their extra
  arguments and return a different value; they are now rejected with the
  expected signature. Too few arguments give the same error instead of an
  `IndexError`, and `EXTRACT` and `DATE_PART` accept only the spec's date
  parts.
- One field or metric whose expression does not translate no longer stops
  the whole model. It is hidden like an object the user may not read,
  everything that depends on it goes with it, and `validate` names each
  one, with or without `--url`.

## [0.1.0] - 2026-09-30

First release. Ossie schema `0.2.0.dev0`, tested on ClickHouse 26.x with
the TPC-DS reference model.

- Model loading on `apache-ossie` with checks upstream lacks: unique names,
  relationships that resolve, expressions in a translatable dialect.
- Expression translation on the SQLGlot AST: the spec's function catalog
  mapped to ClickHouse, `OSSIE_SQL_2026` preferred, `ANSI_SQL` fallback,
  every catalog function checked by value against DuckDB or the spec text.
- Planner: one `SELECT` per question, `LEFT JOIN` along many-to-one
  relationships from one root dataset, `HAVING` for filters over metrics
  or aggregates, window metrics passed through, errors that name the
  nearest known object.
- Executor: `FINAL` for `Replacing*` engines, `dictGetOrNull` for
  single-key dictionaries, both learned from system tables; `inf` and
  `nan` returned as NULL; `{"dedup": "none"}` override in
  `custom_extensions`.
- Access control through ClickHouse: the model is trimmed to what the
  connected user may read; an optional policy file hides more per user or
  role.
- MCP server over stdio with `list_model`, `search_model`,
  `describe_object` and `query`.
- CLI: `validate`, `sql`, `query`, `serve`.
