# Model authoring for ClickHouse

A model for ossie-clickhouse is an ordinary Ossie model: the standard's
[specification](https://github.com/apache/ossie) says what goes in it, and
nothing ClickHouse-specific is required. This page lists what the executor
works out on its own, and the few things worth knowing when the target is
[ClickHouse](https://clickhouse.com/docs). The TPC-DS reference model in
[tests/fixtures/tpcds.yaml](../tests/fixtures/tpcds.yaml) is a complete
example.

## Sources

`source` is `database.table`. A three-part `database.schema.table` is
accepted and the middle part dropped, since ClickHouse has no schema level;
this is what lets a model written for another warehouse run unchanged. A
query as a source is not supported.

## Keys decide which joins are allowed

`primary_key` and `unique_keys` hold physical column names, as the spec
says. They are not decoration: a relationship is used only when its
`to_columns` are the primary key or a unique key of the target dataset.
That is how the planner knows the join is many-to-one and cannot multiply
fact rows. A dimension dataset without a declared key is unreachable.

```yaml
datasets:
  - name: item
    source: tpcds.item
    primary_key: [i_item_sk]
relationships:
  - name: store_sales_to_item
    from: store_sales
    to: item
    from_columns: [ss_item_sk]
    to_columns: [i_item_sk]
```

A question is answered from one root dataset (usually the fact table) with
direct relationships to every other dataset it touches. Chains through an
intermediate dataset and questions across two fact tables are not
supported yet; see the roadmap.

## Expressions

Each field and metric carries expressions in one or more dialects.
`OSSIE_SQL_2026` is preferred, `ANSI_SQL` is the fallback and what models
carry today. Within a field, bare column names are the dataset's own
columns; a field may not reach into another dataset. Metrics reference
fields as `dataset.field`.

The spec's function catalog is translated to ClickHouse. Functions
ClickHouse lacks (`DATEADD`, `DATEDIFF`, `DATE_PART`, `SPLIT_PART`,
`CONTAINS`, `IFF`, `ZEROIFNULL` and others) are rewritten on the syntax
tree; `EXTRACT(DAYOFWEEK ...)` and `DAYOFYEAR` become `toDayOfWeek` and
`toDayOfYear`. Where ClickHouse disagrees with the spec, the spec wins:
`REGEXP_REPLACE` replaces every match, `DAYOFWEEK` is ISO (Monday is 1),
float division by zero and a one-row `STDDEV` come back as NULL, not NaN.
Subqueries and statements inside an expression are rejected.

Window functions over aggregates (`RANK() OVER (...)`, `LAG(SUM(x))`)
pass through in the same `SELECT`. The spec cannot say which grain a
window metric needs, so the question has to supply the right dimensions.
A window metric cannot be used in a filter (ClickHouse does not allow a
window function in `HAVING`); the planner rejects it with a message.

## Time and derived dimensions

The spec has no time grain and no derived-dimension syntax yet, so declare
what you need as fields:

```yaml
- name: sale_month
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: DATE_TRUNC('month', ss_sold_date)
  dimension: {is_time: true}
- name: price_band
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: CASE WHEN ss_sales_price < 10 THEN 'low' WHEN ss_sales_price < 100 THEN 'mid' ELSE 'high' END
```

A calendar table with year, month and day columns, as in TPC-DS, works
just as well.

## Deduplication

Tables with an engine in the
[`Replacing*`](https://clickhouse.com/docs/engines/table-engines/mergetree-family/replacingmergetree)
family keep change history and are read with
[`FINAL`](https://clickhouse.com/docs/sql-reference/statements/select/from#final-modifier), so a
metric sees each row once. This is learned from
[`system.tables`](https://clickhouse.com/docs/operations/system-tables/tables). To read such a table raw (for example, to count
versions), turn it off per dataset:

```yaml
- name: orders_raw
  source: shop.orders
  custom_extensions:
    - vendor_name: CLICKHOUSE
      data: '{"dedup": "none"}'
```

`custom_extensions`, the spec's escape hatch for vendor settings, with
`vendor_name: CLICKHOUSE` is the namespace this project owns; `data` is
JSON. `dedup` is the only key today.

## Dictionaries

A dataset whose source is a ClickHouse [dictionary](https://clickhouse.com/docs/sql-reference/dictionaries)
with a single key, joined on that key, is read with
[`dictGetOrNull`](https://clickhouse.com/docs/sql-reference/functions/ext-dict-functions) and the
join disappears. This is learned from
[`system.dictionaries`](https://clickhouse.com/docs/operations/system-tables/dictionaries), which the connected user needs a grant
to read; without it the dictionary is joined like a table, which is still
correct, only slower.

## Names

Questions resolve names case-insensitively, as the spec requires. The SQL
uses names exactly as the model writes them, because ClickHouse is
case-sensitive: write `source`, keys and columns the way the database
spells them.

## Descriptions and AI hints

`description`, `ai_context.synonyms` and `ai_context.instructions` are
what agents see through `list_model`, `search_model` and
`describe_object`. An agent that reads "gross sales" finds `total_sales`
only if the synonym is there. Write them for the reader who does not know
the schema.

## Validation

```bash
ossie-clickhouse validate model.yaml
ossie-clickhouse validate model.yaml --url http://user:password@host:8123
```

Without `--url`: schema, the pinned version (`0.2.0.dev0`), unique names,
relationships that resolve, expressions in a translatable dialect. With
`--url`: every source exists, every column a field, key or relationship
uses exists. Run it as a user who can read everything the model names;
run as a restricted user it reports that user's view, not the model's
correctness.
