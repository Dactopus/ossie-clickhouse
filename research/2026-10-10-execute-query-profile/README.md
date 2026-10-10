# The `execute_query` profile 0.4-draft (apache/ossie#529) on a ClickHouse executor

Does the executor's MCP tool meet the `execute_query` profile draft,
[apache/ossie#529](https://github.com/apache/ossie/pull/529) `0.4-draft` at
`b5418ee`, which takes the Layer 3 query object of
[apache/ossie#246](https://github.com/apache/ossie/pull/246) at `cc0d070`?
And on a model with two unrelated facts, do refusals carry the #246 codes
where #246 defines them, and do the answers match numbers computed without
the planner?

The check is the profile's own offline checker,
`validation/validate_mcp.py` at `b5418ee`, run on exchanges captured over
the MCP protocol from the server in this repository. It covers the tool's
declarations, the result envelope, the embedded CSV and its encoding, and
the preview; it does not judge the answers. The answers to the two-fact
questions are compared with [expected-two-facts.sql](expected-two-facts.sql).

For Apache Ossie these are the data behind our report to dev@ossie, thread
"[D] Proposal: an MCP server for Ossie".

## Sets

| Captures | Model | Data | Cases |
|---|---|---|---|
| [capture-tpcds.json](capture-tpcds.json) | `tests/fixtures/tpcds.yaml` | TPC-DS as in [CONTRIBUTING.md](../../CONTRIBUTING.md#setup) | 9: answers, refusals with #246 and profile codes, a ClickHouse error |
| [capture-values.json](capture-values.json) | [values.yaml](values.yaml) | [values.sql](values.sql) | 12: NULL, strings starting with a backslash (the string `\N` among them), commas, quotes, a line break, Unicode, the largest `UInt64`, decimals, floats, booleans, dates, instants in two time zones, an array; a result cut at 100 rows; zero rows; arguments outside the schema |
| [capture-two-facts.json](capture-two-facts.json) | `two_facts.yaml`, built by [two_facts.py](two_facts.py) | [commerce.sql](commerce.sql) and the GA4 sample | 15: two unrelated facts (E3013, `E_NO_PATH`), facts sharing a dimension, Having |

The two-fact model joins the `commerce` and `web_analytics` entities of
[dactopus-data-models](https://github.com/Dactopus/dactopus-data-models)
at `f1563d9`, with no relationship between them. [commerce.sql](commerce.sql)
is a copy of the orders, lines and refunds that its Shopify package built
from hand-written test orders; the GA4 side is its public GA4 sample, loaded
into the database `dactopus` as its README's Quick start says. Two GA4
metrics are renamed (`web_revenue`, `web_average_order_value`), since
commerce has metrics of the same names.

## Reproduce

ClickHouse at `$OSSIE_CLICKHOUSE_URL` (default `http://127.0.0.1:8123`) with
the `tpcds` and `dactopus` databases; the setup files create `ossie_profile`.
From the repository root, with `uv sync --extra mcp`:

```bash
D=research/2026-10-10-execute-query-profile
uv run python $D/capture.py tests/fixtures/tpcds.yaml $D/cases-tpcds.json $D/capture-tpcds.json --url http://127.0.0.1:8123/tpcds
uv run python $D/capture.py $D/values.yaml $D/cases-values.json $D/capture-values.json --setup $D/values.sql
uv run python $D/two_facts.py ../dactopus-data-models/entities ossie_profile dactopus $D/two_facts.yaml
uv run python $D/capture.py $D/two_facts.yaml $D/cases-two-facts.json $D/capture-two-facts.json --setup $D/commerce.sql
```

Then the checker, from a clone of apache/ossie at `../ossie`:

```bash
git -C ../ossie fetch https://github.com/apache/ossie pull/529/head && git -C ../ossie checkout b5418ee
for s in tpcds values two-facts; do
  uv run --no-project --with jsonschema==4.26.0 python ../ossie/validation/validate_mcp.py $D/capture-$s.json > $D/report-$s.json
done
```

The captures and reports here are the run of 2026-10-10: this branch at
`bf34e51`, ClickHouse 26.9.5.2, the MCP Python SDK 2.2.0. The model
revision in each `data_source_id` is a hash of the model, so another model
file gives another id.

## Results

| Set | Checker | Cases | Findings | Unverified |
|---|---|---|---|---|
| TPC-DS | `passed` | 9 | 0 | 0 |
| Edge values | `passed` | 12 | 0 | 0 |
| Two facts | `passed` | 15 | 0 | 0 |

Before the branch followed the profile's result format, the same nine
TPC-DS exchanges gave 42 findings, all in `structuredContent`.

The two-fact questions ([cases-two-facts.json](cases-two-facts.json),
expected values in [expected-two-facts.txt](expected-two-facts.txt)):

| Case | Executor | #246 |
|---|---|---|
| A1, A4: measures of `orders` and `sessions`, no dimension or one of `orders` | `E3013_NO_STITCHING_DIMENSION` | same |
| A2, A3: a measure of `orders` by or filtered on `sessions.country` | `E_NO_PATH` | same |
| B1, B4: `order_lines` and `refunds` measures, by channel or in total | `UNSUPPORTED_QUERY` | answered: each fact aggregated on its own, then joined |
| B2: an `orders` measure with an `order_lines` measure | `UNSUPPORTED_QUERY` | answered, as B1 |
| B3: `purchases` and `sessions` measures by source | `UNSUPPORTED_QUERY` | answered, as B1 |
| C1, C2, C5, C7: Having on a metric, a joined fact's metric, a dimension, an unselected metric | answers equal to the expected values | same |
| C3, C4, C6: a row condition in Having, an aggregate in Where, both levels in one predicate | `E_NON_AGGREGATE_IN_HAVING`, `E_AGGREGATE_IN_WHERE`, `E_MIXED_PREDICATE_LEVEL` | same |

Found on the way, outside the checker's scope:

- #246 calls a query mixed when both Fields and Dimensions or Measures are
  non-empty (section 6.2, step A.1); the profile does when the keys are
  present, even if empty. The executor follows the profile.
- The MCP Python SDK 2.2.0 drops unknown arguments and decodes a `query`
  sent as a JSON string before the tool runs; the executor reads the
  arguments as sent to refuse both with `INVALID_ARGUMENT`.
- Claude Code 2.1.216 shows an agent both the CSV and `structuredContent`
  of a success and drops the text block; of a tool error it shows only the
  text, so a repair hint has to be in `error.message` too. The preview
  therefore holds 10 rows.
- ClickHouse's own CSV quotes the string `\N` where the profile writes
  `\\N`, so the executor encodes CSV itself.
