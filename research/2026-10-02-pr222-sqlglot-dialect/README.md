# The Ossie SQLGlot dialect (apache/ossie PR #222) against ClickHouse

Does the Ossie SQLGlot dialect from
[apache/ossie#222](https://github.com/apache/ossie/pull/222) change the
ClickHouse SQL that SQLGlot generates from Ossie expressions, and how much of
the distance between the Ossie expression language and ClickHouse comes from
SQLGlot's ClickHouse generator rather than from the spec? The corpus is 143
samples from the spec's function catalog ([corpus.yaml](corpus.yaml)) plus
the 39 field and metric expressions of the TPC-DS reference model
(`examples/tpcds_semantic_model.yaml` in apache/ossie at adfa9e4, Apache
License 2.0; `tests/fixtures/tpcds.yaml` here is an identical copy). Each one
is parsed with SQLGlot's default dialect, with `read="ossie"` and with
`read="snowflake"` as a third reading, then generated for ClickHouse and
executed. Where the SQL differs from the Ossie dialect's and both run, the
full results are compared.

For ossie-clickhouse this backs the decision not to depend on the unmerged
branch. The only output change that matters is `DATEDIFF`, where the spec
puts the date part first; we parse it that way ourselves in
[translate.py](https://github.com/Dactopus/ossie-clickhouse/blob/78ec85a/src/ossie_clickhouse/translate.py#L66-L105),
and switching to `read="ossie"` is a one-line change once the PR merges.
The first run on 2026-09-30 used SQLGlot 30.20.0; this rerun checks the
decision on 30.21.0.

For Apache Ossie these are the data behind our
[review comment on PR #222](https://github.com/apache/ossie/pull/222#issuecomment-5926761044)
(2026-10-01): what a Python/SQLGlot implementation of the spec costs on one
target engine.

To reproduce: ClickHouse with the `tpcds` database loaded as in
[CONTRIBUTING.md](../../CONTRIBUTING.md#setup), at `$OSSIE_CLICKHOUSE_URL`
(default `http://127.0.0.1:8123`). [results.txt](results.txt) is the run of
2026-10-02 with SQLGlot 30.21.0, PR #222 at head 5ddda46 and ClickHouse
26.9.5. The same commands with `sqlglot==30.20.0` or SQLGlot `main` at
954503e produced [results-sqlglot-30.20.0.txt](results-sqlglot-30.20.0.txt)
and [results-sqlglot-main.txt](results-sqlglot-main.txt). Run from this
folder:

```bash
uv venv
uv pip install sqlglot==30.21.0 clickhouse-connect==1.9.0 pyyaml==6.0.3 "apache-ossie-sql @ git+https://github.com/apache/ossie.git@5ddda4618ef061e35adae680db279d38b44324f3#subdirectory=core/python"
.venv/bin/python evaluate.py ../../tests/fixtures/tpcds.yaml > results.txt
```

## Results

| Measure | Result |
|---|---|
| Parse failures with `read="ossie"` | 0 of 182 |
| ClickHouse SQL identical to the default dialect | 172 of 182 |
| Ossie dialect runs where the default fails | 3 (`DATEDIFF`, all three units) |
| Default runs where the Ossie dialect fails | 0 |
| Output differs, same outcome | 7 (`DATE_PART` -> `EXTRACT`, `DATEADD` -> `DATE_ADD`, `APPROX_PERCENTILE` -> `APPROX_QUANTILE`, which ClickHouse lacks too) |
| TPC-DS model expressions that run on ClickHouse | 39 of 39 in each of the three dialects |
| Runs in both, result differs from the Ossie dialect | default 0, snowflake 2 |
| False rejections by `validate_expression` | 0 of 182 |
| Disallowed constructs rejected | 6 of 6 (`SELECT`, subquery in `IN`, `EXISTS`, `GROUPS` frame, `?` and `:p` placeholders) |
| Code issues from the PR review that reproduce | 3 of 3 (`NOT IN` by string replacement, `CHARINDEX` third argument dropped, `APPROX_PERCENTILE` accuracy dropped) |

Where ClickHouse rejects the generated SQL, or SQLGlot drops an argument,
`evaluate.py` labels the cause from the parse tree: `parser` when only the
default dialect fails, `generator` when SQLGlot knows every function in the
expression and its ClickHouse generator gets it wrong, `unknown` when
SQLGlot passes a function through by name. `parser` is defined for the pair
default and `ossie` only; the `snowflake` column shows which catalog
functions SQLGlot already models, read from its `unknown`.

| Cause | default | `ossie` | `snowflake` |
|---|---|---|---|
| `parser` | 3 | 0 | - |
| `generator` | 11 | 12 | 14 |
| `unknown` | 7 | 6 | 0 |

With the Ossie dialect, 12 of the 18 divergences from ClickHouse come from
SQLGlot's ClickHouse generator, not from the spec: `PERCENTILE_CONT` and
`PERCENTILE_DISC ... WITHIN GROUP`, `APPROX_QUANTILE`, `DAYOFYEAR`,
`EXTRACT(DAYOFWEEK | DAYOFYEAR ...)`, `TO_CHAR` (format dropped),
`SPLIT_PART`, `CONTAINS`, `REGEXP_COUNT`, and `CURRENT_TIME` in two forms;
for the last, the Snowflake dialect generates `LOCALTIME`, which ClickHouse
accepts. The Ossie dialect has one more than the default because it maps
`APPROX_PERCENTILE` to `ApproxQuantile`, for which the ClickHouse generator
has no rule, so the function moves from `unknown` to `generator`. Closed
upstream so far:
`VAR_POP` in [tobymao/sqlglot#8469](https://github.com/tobymao/sqlglot/pull/8469)
(released in 30.21.0; 13 on 30.20.0) and `DAYOFYEAR` in
[tobymao/sqlglot#8473](https://github.com/tobymao/sqlglot/pull/8473)
(merged, not yet released; 11 on `main`);
[tobymao/sqlglot#8474](https://github.com/tobymao/sqlglot/pull/8474)
(merged, not yet released) fixes the DuckDB generator this project uses for
expected values in tests. More in preparation.

The Ossie dialect leaves six catalog functions `unknown`: `TO_DATE` in both
forms, `TO_TIMESTAMP`, `IFF`, `ZEROIFNULL`, `NULLIFZERO`. The Snowflake
dialect models all six. Five run on ClickHouse: `IFF`, `ZEROIFNULL` and
`NULLIFZERO` become `CASE`, `TO_DATE` and `TO_TIMESTAMP` without a format
become `CAST`. `TO_DATE` with a format becomes `STR_TO_TIME`, which ClickHouse
lacks (`generator`). In the other rows of the `snowflake` column, `DATEADD`
and `DATEDIFF` run (Snowflake puts the date part first, as the spec does),
`CURRENT_TIME` runs as `LOCALTIME`, and `DATE_PART('year', d)` becomes
`EXTRACT('year' FROM d)`, which ClickHouse rejects, in all three samples; the
rule counts these as `generator`, the string date part comes from the
Snowflake parser. Overall 168 of 182 are `ok` with `snowflake`, 164 with
`ossie`.

Default and `ossie` sort NULL as the smallest value, `snowflake` as the
largest; in ClickHouse SQL the first two add `NULLS FIRST` to ascending
window orderings, the third to descending ones. `evaluate.py` runs metrics
over `LEFT JOIN`s with `join_use_nulls = 1`, as the ossie-clickhouse planner
does, so `store_sales` rows without a matching date get a NULL `d_date`.
In `cumulative_sales`, a running `SUM` ordered by `d_date`, the running total
on the NULL row is 124,556,714.61 with the Ossie dialect, which sums it
first, and 5,262,470,646.52 with the Snowflake dialect, which sums it last.
In `monthly_sales_change`, the change against the previous month, January
1998 is -75,399,822.72 with the Ossie dialect, whose previous row is the
NULL one, and NULL with the Snowflake dialect. The spec
(`expression_language.md`, `spec.md`, `spec.yaml` at adfa9e4) does not
define NULL ordering.
