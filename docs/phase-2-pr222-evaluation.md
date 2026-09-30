# PR #222 evaluation: the Ossie SQLGlot dialect against ClickHouse

Date: 2026-09-30. PR head 5ddda46 (2026-08-11), SQLGlot 30.20, ClickHouse
26.9.5. Script and raw output in [spikes/phase2](../spikes/phase2).

## Decision

Do not depend on the branch. Parse with SQLGlot's default dialect and
replicate the one behaviour that changes ClickHouse output, the leading
date-part argument of `DATEADD`, `DATEDIFF` and `DATE_PART`, in a few lines of
our own parser setup. Keep the parse step isolated so that switching to
`read="ossie"` is a one-line change once the PR merges. The function mapping
table for ClickHouse stays our work either way: the dialect is a parser and
validator for the spec's SQL, not a generator for any target engine.

## What the package is

About 720 lines: a SQLGlot dialect that starts from the default ANSI-like
parser and generator and overrides a handful of spec constructs; a validator
that rejects constructs the spec disallows; a registry of function names by
compliance level; identifier normalization rules; window frame checks. Its
own 143 tests pass on SQLGlot 30.20.

## Results on the Phase 0 corpus (182 expressions)

| Measure | Result |
|---|---|
| Parse failures with `read="ossie"` | 0 |
| ClickHouse output identical to default dialect | 172 |
| Output differs and is better | 3 (`DATEDIFF`, all three units) |
| Output differs and is worse | 0 |
| Output differs, equivalent | 7 (`DATE_PART` -> `EXTRACT`, `DATEADD` -> `DATE_ADD`, `APPROX_PERCENTILE` -> `APPROX_QUANTILE`) |
| False rejections by `validate_expression` | 0 of 182 |
| Forbidden constructs rejected | 6 of 6 (`SELECT`, subquery in `IN`, `EXISTS`, `GROUPS` frame, `?` and `:p` placeholders) |

The `DATEDIFF` fix matters: the default dialect treats the last argument as
the unit and emits `DATE_DIFF(D2, d, day)`, which fails in ClickHouse. The
Ossie dialect knows the spec puts the unit first.

`APPROX_QUANTILE` does not exist in ClickHouse (it has `quantile`). That is a
gap in SQLGlot's ClickHouse generator, not in the PR, and lands in our
mapping table.

## Review comments on the PR, reproduced

The change request from 2026-09-27 lists three code issues. All three
reproduce on the branch head:

- **`NOT IN` by string replacement** (`dialect.py`, `not_sql`). Input
  `'x IN y' NOT IN ('a')` renders in the Ossie dialect as
  `'x NOT IN y' IN ('a')`: the replacement hits the string literal, and the
  meaning changes. Same input renders correctly for ClickHouse, because that
  generator does not use the override.
- **`CHARINDEX` third argument dropped** (`strposition_sql`). Input
  `CHARINDEX('a', s, 3)` renders as `POSITION('a' IN s)`; ClickHouse output
  keeps the `3`.
- **`APPROX_PERCENTILE` accuracy dropped** (`approxquantile_sql`). Input
  `APPROX_PERCENTILE(x, 0.5, 100)` renders as `APPROX_PERCENTILE(x, 0.5)`.

The rebase conflict and missing ASF headers were not checked; they are
stated in the review and are mechanical.

## Finding for our own design

The spec, and the PR's `identifiers.py`, normalize unquoted identifiers to
upper case: `store_sales.ss_ext_sales_price` resolves as
`STORE_SALES.SS_EXT_SALES_PRICE`. ClickHouse identifiers are case-sensitive.
Name resolution against the model must be case-insensitive, and generated
SQL must carry the physical column name in its real case, never the
normalized one. The dialect's own rendering leaves case untouched, so this
is a rule for our planner, not a bug in the PR.

## Draft comment for the PR

> We are building an Ossie implementation for ClickHouse
> (github.com/Dactopus/ossie-clickhouse) and ran this branch against our
> test corpus: the full function catalog from `expression_language.md`
> (143 sample expressions) plus every expression in
> `examples/tpcds_semantic_model.yaml`, generating ClickHouse SQL from the
> parsed AST and executing it.
>
> Results at 5ddda46 with SQLGlot 30.20: zero parse failures, zero false
> rejections from `validate_expression`, all six disallowed constructs we
> tried rejected. Compared with SQLGlot's default dialect the ClickHouse
> output is identical for 172 of 182 expressions and strictly better for
> `DATEDIFF` (the default dialect misplaces the unit argument). Nothing got
> worse.
>
> The three code issues from the review reproduce on the head commit:
> `'x IN y' NOT IN ('a')` renders as `'x NOT IN y' IN ('a')`;
> `CHARINDEX('a', s, 3)` loses the `3`; `APPROX_PERCENTILE(x, 0.5, 100)`
> loses the `100`.
>
> Happy to send a PR to this branch with the rebase, ASF headers and the
> three fixes plus tests if that helps get it over the line.
