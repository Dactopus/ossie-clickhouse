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

## Phase 3: Query planner, minimum (done)

Completed 2026-09-30. `planner.py` takes metrics, dimensions and filters,
resolves `dataset.field` references case-insensitively, inlines field
expressions, picks the one root dataset with direct relationships to every
other dataset used, and emits one `SELECT` with `LEFT JOIN`s and
`SETTINGS join_use_nulls = 1`. Joins are allowed only when `to_columns`
match a primary or unique key of the target (inferred many-to-one; the spec
has no cardinality attribute). Window metrics over aggregates pass through
in the same `SELECT`; the spec cannot declare a metric's required grain, so
that is not checked. Expressions are rejected if they contain subqueries or
statements. Errors name the nearest known metric, field or dataset.
CLI: `ossie-clickhouse sql`. CI: GitHub Actions with a ClickHouse service.

Tests: SQL snapshots, error messages, and execution against TPC-DS,
including a check that grouping by joined dimensions neither multiplies nor
drops fact rows.

Deferred to Phase 8: multi-fact queries, joins that are not many-to-one,
metrics over metrics, filters on aggregates (`HAVING`), `source` as a query.

## Phase 4: Executor (done)

Completed 2026-09-30. `executor.py` connects through `clickhouse-connect`,
introspects `system.tables`, `system.columns` and `system.dictionaries`
for the model's sources, and hands the planner a catalog. Tables with an
engine in the `Replacing*` family are read with `FINAL`: measured on
3.5 million rows with 20% duplicate versions, `FINAL` was faster than an
`argMax` rewrite (94 ms against 150 ms alone, 90 against 125 with a join)
and far simpler to generate, so `argMax` was not built. Single-key
dictionaries joined on their key are read with `dictGetOrNull` and the
join is dropped. Per-dataset overrides come from `custom_extensions` with
`vendor_name: CLICKHOUSE` and JSON data, currently `{"dedup": "none"}`.
NaN in result sets becomes NULL. `validate --url` checks sources and
columns against the database. CLI: `ossie-clickhouse query` with TSV or
`--json` output.

Tests create a `ReplacingMergeTree` fact table and a dictionary in a test
database and check deduplicated sums, dictionary reads, the override, NaN
handling, and database-level validation messages.

## Phase 5: Access control (done)

Completed 2026-09-30. ClickHouse is the source of truth: the executor runs
as whoever the client is connected as, so grants, column grants and row
policies apply on their own. ClickHouse lists in `system.tables` and
`system.columns` only what the connected user may read, so the catalog the
executor already builds doubles as the visibility map: datasets whose
source is absent and fields whose columns are absent are removed from the
model, and relationships and metrics that depend on them go with them
(`access.py`, `restrict()`). `system.dictionaries` needs an explicit grant;
without it dictionaries are simply joined as tables. An optional policy
file (`--policy`) hides datasets, fields or metrics per ClickHouse user or
role, matched against `currentUser()` and `enabledRoles()`. Identity is the
connection's credentials (`http://user:password@host:8123`); nothing here
authenticates anyone. `validate --url` (`check()`) is a model owner's tool
and should run with full read rights.

Tests create restricted users, a role and a row policy and check: a
dataset the user cannot read is absent from the model and from
suggestions; row policies change results; column grants hide fields and
the relationships and metrics resting on them; the policy file hides a
metric for a role and only that role.

## Phase 6: MCP server, local (done)

Completed 2026-09-30. `mcp_server.py` on the MCP Python SDK 2.x, behind the
`[mcp]` extra; `ossie-clickhouse serve <model>` runs it over stdio with
ClickHouse credentials from the environment or `--url`, so one process is
one ClickHouse user and Phase 5 applies unchanged. Four tools, each a
library call: `list_model` (compact summary with descriptions and
synonyms), `search_model` (by name, description or synonym),
`describe_object` (dataset with fields and relationships, or metric with
its expression, with AI hints), `query` (rows plus the SQL, sorted by the
first metric unless `order_by` says otherwise, because agents that rank or
total rows themselves get it wrong). Errors are
returned as results with an `error` field so the planner's suggestions
reach the agent verbatim. The "who is calling" seam is `client_factory`
in `build_server()`. Setup for desktop agents in
[docs/mcp-setup.md](docs/mcp-setup.md).

Tests: the summary, search and describe views against the library
directly; the four tools through the SDK's in-memory client against
TPC-DS; a raw stdio handshake was checked by hand.

## Phase 6b: MCP server, remote (after the first release)

Streamable HTTP with OAuth 2.1, built only when a real deployment asks for
it. Design fixed now so Phase 6 does not paint us into a corner:

- The server validates bearer tokens from the organization's identity
  provider (Okta, Entra ID, Keycloak) through the SDK's token verifier;
  the SDK serves the RFC 9728 metadata. The MCP token is never passed to
  ClickHouse.
- Caller identity to ClickHouse identity, two strategies behind the seam
  from Phase 6:
  - Token exchange: obtain a ClickHouse-audience JWT for the user and
    connect with it. Works with ClickHouse Cloud JWT authentication
    (Enterprise plan, 26.4+), where users and roles come from token claims,
    and with self-hosted builds that verify JWTs (Altinity Antalya, or the
    HTTP external authenticator with a JWT-verifying sidecar).
  - Role switching: connect as a service user granted every role and
    enable, per request, only the roles mapped from the token's groups.
    Works with any ClickHouse; grants, row policies and the Phase 5
    trimming all follow the active roles. The group-to-role mapping is the
    one configuration a team writes.
- Enterprise-Managed Authorization extension: support once clients carry
  it; it changes how the token is obtained, not how it is verified.

## Phase 7: First public release

- Packaging and publication to PyPI.
- Documentation: installation, model authoring for ClickHouse, CLI, MCP
  setup with desktop agents, access control through ClickHouse.
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
