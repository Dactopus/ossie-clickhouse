# Design decisions

Decisions that rest on a measurement or an evaluation, recorded so they are
not relitigated without new evidence. The rules themselves are in
[AGENTS.md](../AGENTS.md); the reasoning is here.

## `FINAL` instead of `argMax` for `Replacing*` tables

Measured on 3.5 million rows with 20% duplicate versions: `FINAL` took
94 ms against 150 ms for an `argMax` rewrite alone, 90 against 125 with a
join. `FINAL` is also one keyword to generate where `argMax` is a subquery
per dataset. `argMax` was not built.

## Parsing with SQLGlot's default dialect, not the upstream Ossie dialect

apache/ossie PR #222 adds an Ossie dialect to SQLGlot. Evaluated and not
depended on: it parses and validates the spec's SQL, but the mapping to
ClickHouse stays this project's work either way. Switch parsing to it once
it merges; the rewrite layer is unchanged.

## SQLGlot bounds move only in minor releases

Some rewrites in `translate.py` cover gaps in SQLGlot's ClickHouse
generator, and fixes for them go upstream. A rewrite left in place after
SQLGlot is fixed does no harm: it runs before generation and yields the
same SQL. Removing it means raising the lower bound on `sqlglot`, and every
raise makes the package harder to install next to tools that pin SQLGlot
narrowly. So patch releases never change the bounds. Each minor release
raises the lower bound to the current SQLGlot release and drops the
rewrites it made redundant; the value tests stay. The upper bound moves to
a new SQLGlot major only after the tests pass on it.

## Spec over engine where they disagree

DuckDB supplies expected values in tests as a second opinion close to the
spec's Postgres-like semantics, not as ground truth. Where DuckDB and the
spec differ, the test pins the spec's value with a section reference:
`REGEXP_REPLACE` replaces every match, `DAYOFWEEK` is ISO (Monday is 1),
`MILLISECOND` is the component, `PERCENTILE_DISC` is the first value whose
cumulative share reaches `p` (ClickHouse's `quantileExact` picks one too
high whenever `p * n` is whole).

## NULLs sort last in both directions

The spec does not define where NULLs go in `ORDER BY`, and its window
syntax (`ORDER BY order_expr [ASC|DESC]`) has no `NULLS FIRST | LAST` for
the model author to say it (checked in `expression_language.md`,
`spec.md` and `spec.yaml` at apache/ossie b6c702e). Engines differ:
ClickHouse 26.9 and DuckDB 1.5.6 put NULLs last both ways; Postgres treats
NULL as larger than any value, last ascending and first descending;
SQLGlot's default dialect, which the parser derives from, treats it as
smaller, first ascending. The translator used to inherit that last one,
and the planner matched it, so a running total ordered by a key with NULLs
started from their sum: on TPC-DS, with `LEFT JOIN`s, the 129,850 sales
without a matching date came first in `cumulative_sales`.

Chosen: NULLs last both ways. It is ClickHouse's default, so the generated
SQL carries no `NULLS` clause, and it agrees with DuckDB, the reference
engine in tests, so no expected value needs pinning. For a query's
`order_by` it also keeps rows without a value at the end of a top-N list
in either direction. The parser dialect sets `NULL_ORDERING`; the Ossie
dialect from apache/ossie PR #222 inherits SQLGlot's default, so switching
to it must keep the setting. An explicit `NULLS FIRST | LAST` is rejected,
as the spec's syntax has none. Reopen if the spec defines NULL ordering.

NaN is not NULL and engines disagree on it too: ClickHouse puts NaN after
the values and before NULLs in both directions; DuckDB and Postgres treat
it as larger than any value, so first descending. The executor returns NaN
as `None`, so ClickHouse's order keeps every row without a value at the
end. A test ordering by a key that can be NaN pins its expected value by
hand.

## Window frames limited to the spec's

The spec lists three frames: `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT
ROW`, `ROWS BETWEEN n PRECEDING AND n FOLLOWING` and `RANGE BETWEEN
UNBOUNDED PRECEDING AND CURRENT ROW`; its examples also use `n PRECEDING
AND CURRENT ROW`. Accepted: `ROWS` frames that hold the current row, and
that one `RANGE` frame. The others give wrong values in ClickHouse 26.9,
compared with DuckDB on keys with NULLs:

- An empty frame, such as `ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING` on
  the last row, sums to 0 (`AVG` to NaN), not NULL.
- `RANGE` with an offset counts NULL keys as within any offset: on keys
  (1, NULL, 2, NULL, 2, 4), `SUM(v) OVER (ORDER BY k RANGE BETWEEN 1
  PRECEDING AND 1 FOLLOWING)` adds the NULL rows to the row with key 4.

`parse()` rejects other frames and `GROUPS`, so such a field or metric is
hidden and reported by `validate`. Reopen when the translator can rewrite
them, or ClickHouse fixes them.

## Many-to-one inferred from keys

The spec has no cardinality attribute. A relationship is used only when its
`to_columns` are the primary key or a unique key of the target, which is
what makes a `LEFT JOIN` safe against fan-out. Star schemas need nothing
more; one-to-many and multi-fact questions wait for demand.

## Remote MCP server, designed, not built

Streamable HTTP with OAuth 2.1 is built only when a real deployment asks
for it. The design is fixed so the local server does not preclude it:

- The server validates bearer tokens from the organization's identity
  provider through the SDK's token verifier; the SDK serves the RFC 9728
  metadata. The MCP token is never passed to ClickHouse.
- Caller identity maps to a ClickHouse identity behind `client_factory`,
  the one seam that decides which connection a caller gets. Two
  strategies: token exchange for a ClickHouse-audience JWT (ClickHouse
  Cloud JWT authentication, or self-hosted builds that verify JWTs), or
  role switching, where a service user holds every role and each request
  enables only the roles mapped from the token's groups. Role switching
  works with any ClickHouse; grants, row policies and model trimming all
  follow the active roles.
