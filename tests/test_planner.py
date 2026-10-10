import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.planner import (
    Code,
    Order,
    PlanError,
    Planner,
    Query,
    Suggestion,
    TableInfo,
    source_name,
    source_table,
    unanswerable_metrics,
)
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)
P = Planner(MODEL)


def test_source_table():
    assert source_table("tpcds.public.store_sales").sql() == "tpcds.store_sales"
    assert source_table("db.t").sql() == "db.t"
    assert source_table("t").sql() == "t"
    assert source_table("db.select").sql(dialect="clickhouse") == "db.select"
    assert source_table("db.x-y").sql(dialect="clickhouse") == 'db."x-y"'
    # Unquoted text is the name it spells, as in 0.2.3, next to quoted parts too.
    assert source_table("db.t$x").sql(dialect="clickhouse") == 'db."t$x"'
    assert source_table("db.t#1").sql(dialect="clickhouse") == 'db."t#1"'
    assert source_table('"my db".t$x').sql(dialect="clickhouse") == '"my db"."t$x"'
    assert source_table("`shop`.t#1").sql(dialect="clickhouse") == '"shop"."t#1"'
    assert source_table("  db.t ").sql() == "db.t"


@pytest.mark.parametrize(
    "source",
    [
        "SELECT * FROM t",
        "db.t x",
        "db . t",
        "numbers(10)",
        "s3('x')",
        "db.t(1)",
        # what refuses a table name must not hide that a query is one
        "SELECT 1;",
        "SELECT * FROM t -- x",
        "SELECT replaceRegexpAll(s, '\\d', '') FROM t",
        "SELECT * FROM t WHERE s = 'say \"hi'",
    ],
)
def test_query_source_refused(source):
    # Table functions included: one such dataset must not break introspection.
    with pytest.raises(PlanError, match="query sources"):
        source_table(source)


@pytest.mark.parametrize(
    "source",
    [
        "db.",
        ".t",
        "db..t",
        "a.b.c.d",
        "`db`.``",
        "`db`.``.t",
        "db.x`y`",
        "db.t;",
        "db.t -- legacy",
        "/* x */ db.t",
        "db.t--legacy",
        "db.t/*x*/",
        "db.o'brien",
        '"my db.t',
        "db.a\\",
        "`db`.`a\\x41`",  # ClickHouse reads `aA`; emitted unescaped, it would differ
        'db."t',
    ],
)
def test_garbled_source_refused(source):
    # Refused, not read as a neighbouring table with the rest dropped.
    with pytest.raises(PlanError, match="cannot map"):
        source_table(source)


@pytest.mark.parametrize(
    "source, db, name",
    [
        ("`shop`.`orders`", "shop", "orders"),  # as dbt-clickhouse writes it
        ("`my.db`.`orders`", "my.db", "orders"),
        ('"Sales DB".orders', "Sales DB", "orders"),
        ('`a`."b.c".`t`', "a", "t"),
        ('"a""b".`c``d`', 'a"b', "c`d"),
        ("`x (1)`.`t--1;`", "x (1)", "t--1;"),
    ],
)
def test_source_table_quoted(source, db, name):
    t = source_table(source)
    assert (t.db, t.name) == (db, name)
    assert source_table(t.sql(dialect="clickhouse")) == t  # quotes kept in SQL
    assert source_name(source) == f"{db}.{name}"


def test_backslash_in_a_column_name(tmp_path):
    # ClickHouse reads `\` in a quoted name as an escape: emitted as written,
    # "a\x41" would read column aA. The server-side check is in test_executor.
    p = tmp_path / "esc.yaml"
    p.write_text(
        'version: "0.2.0.dev0"\nname: esc\ndatasets:\n'
        "  - name: esc\n    source: db.esc\n    fields:\n"
        "      - name: v\n"
        "        expression: {dialects: [{dialect: ANSI_SQL, expression: '\"a\\x41\"'}]}\n"
        "metrics:\n"
        "  - name: total\n"
        "    expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(esc.v)}]}\n"
    )
    sql = Planner(load_model(p)).sql(Query(measures=("total",), dimensions=("esc.v",)))
    assert sql.startswith('SELECT esc."a\\\\x41" AS v, SUM(esc."a\\\\x41") AS total FROM')


def test_single_dataset_no_join():
    sql = P.sql(Query(measures=("total_sales",)))
    assert sql == (
        "SELECT SUM(store_sales.ss_ext_sales_price) AS total_sales "
        "FROM tpcds.store_sales AS store_sales SETTINGS join_use_nulls = 1"
    )


def test_dimension_filter_join_snapshot():
    sql = P.sql(
        Query(
            measures=("total_sales", "total_profit"),
            dimensions=("item.i_brand",),
            where=("date_dim.d_year = 1998",),
            limit=10,
        )
    )
    assert sql == (
        "SELECT item.i_brand AS i_brand, "
        "SUM(store_sales.ss_ext_sales_price) AS total_sales, "
        "SUM(store_sales.ss_net_profit) AS total_profit "
        "FROM tpcds.store_sales AS store_sales "
        "LEFT JOIN tpcds.date_dim AS date_dim ON store_sales.ss_sold_date_sk = date_dim.d_date_sk "
        "LEFT JOIN tpcds.item AS item ON store_sales.ss_item_sk = item.i_item_sk "
        "WHERE date_dim.d_year = 1998 "
        "GROUP BY item.i_brand ORDER BY total_sales DESC LIMIT 10 SETTINGS join_use_nulls = 1"
    )


def test_field_expression_is_inlined_and_qualified():
    sql = P.sql(Query(dimensions=("customer.customer_full_name",), measures=("total_sales",)))
    expr = "customer.c_first_name || ' ' || customer.c_last_name"
    assert f"{expr} AS customer_full_name" in sql and f"GROUP BY {expr}" in sql


def test_case_insensitive_resolution_keeps_physical_case():
    sql = P.sql(Query(measures=("TOTAL_SALES",), dimensions=("ITEM.I_BRAND",)))
    assert "item.i_brand AS i_brand" in sql and "SUM(store_sales.ss_ext_sales_price)" in sql


def test_dimension_only_query():
    sql = P.sql(Query(dimensions=("store.s_state",)))
    assert sql.startswith("SELECT store.s_state AS s_state FROM tpcds.store AS store GROUP BY")


def test_window_metric_passes_through():
    sql = P.sql(Query(measures=("cumulative_sales",), dimensions=("date_dim.d_date",)))
    assert "SUM(SUM(store_sales.ss_ext_sales_price)) OVER (ORDER BY date_dim.d_date" in sql


def test_order_by():
    # Default: first measure descending, NULLs last, only when there are rows to rank.
    q = Query(measures=("total_profit", "total_sales"), dimensions=("item.i_brand",))
    assert "ORDER BY total_profit DESC SETTINGS" in P.sql(q)
    assert "ORDER BY" not in P.sql(Query(measures=("total_sales",)))
    assert "ORDER BY" not in P.sql(Query(dimensions=("item.i_brand",)))
    # Explicit order: NULL sorts as the highest value (#246 §5.1), so first descending.
    q = Query(
        measures=("total_sales",),
        dimensions=("item.i_brand",),
        order_by=(Order("total_sales", "DESC"),),
    )
    assert P.sql(q).endswith(
        "GROUP BY item.i_brand ORDER BY total_sales DESC NULLS FIRST SETTINGS join_use_nulls = 1"
    )
    q = Query(
        measures=("total_sales",),
        dimensions=("item.i_brand",),
        order_by=(Order("item.i_brand"), Order("TOTAL_SALES", "asc")),
    )
    # Ascending, NULLs last: ClickHouse's default, so no NULLS clause.
    assert "ORDER BY i_brand ASC, total_sales ASC SETTINGS" in P.sql(q)
    q = Query(
        measures=("total_sales",),
        dimensions=("item.i_brand",),
        order_by=(Order("total_sales", "DESC", "last"), Order("i_brand", "ASC", "FIRST")),
    )
    assert "ORDER BY total_sales DESC, i_brand ASC NULLS FIRST SETTINGS" in P.sql(q)
    # The caller's spelling of dataset.field never leaks into ORDER BY: aliases are the model's.
    q = Query(dimensions=("date_dim.D_YEAR",), order_by=(Order("DATE_DIM.D_YEAR"),))
    assert "AS d_year" in P.sql(q) and "ORDER BY d_year ASC" in P.sql(q)


@pytest.mark.parametrize(
    ("order", "code", "message"),
    [
        (
            Order("total_sale"),
            Code.E_NAME_NOT_FOUND,
            "order_by 'total_sale' is not a selected measure or dimension "
            "(did you mean: total_sales?)",
        ),
        # i_brand is selectable as "i_brand" and "item.i_brand"; the hint names it once.
        (Order("brand"), Code.E_NAME_NOT_FOUND, "(did you mean: i_brand?)"),
        (Order("total_sales", "down"), Code.QUERY_INVALID, "direction is ASC or DESC"),
        (Order("total_sales", "DESC", "middle"), Code.QUERY_INVALID, "nulls FIRST or LAST"),
        # Known but not selected, or an expression: #246 allows both, this version not.
        (Order("total_profit"), Code.UNSUPPORTED_QUERY, "select it to order by it"),
        (Order("store.s_state"), Code.UNSUPPORTED_QUERY, "select it to order by it"),
        (Order("SUM(store_sales.ss_quantity)"), Code.UNSUPPORTED_QUERY, "only by names"),
    ],
)
def test_order_by_refusals(order, code, message):
    q = Query(measures=("total_sales",), dimensions=("item.i_brand",), order_by=(order,))
    with pytest.raises(PlanError) as e:
        P.sql(q)
    assert e.value.code == code and message in str(e.value)


def test_order_parse():
    assert Order.parse("total_sales") == Order("total_sales")
    assert Order.parse("total_sales desc") == Order("total_sales", "DESC")
    assert Order.parse("total_sales DESC nulls last") == Order("total_sales", "DESC", "LAST")
    assert Order.parse("item.i_brand nulls first") == Order("item.i_brand", "ASC", "FIRST")
    for bad in ("", "total_sales down", "total_sales desc nulls", "a desc nulls last x"):
        with pytest.raises(PlanError, match="use 'name'"):
            Order.parse(bad)


def test_deterministic():
    q = Query(measures=("total_sales",), dimensions=("store.s_state", "item.i_category"))
    assert P.sql(q) == P.sql(q)


@pytest.mark.parametrize(
    ("query", "code", "message"),
    [
        (
            Query(measures=("total_sale",)),
            Code.E_NAME_NOT_FOUND,
            "unknown metric 'total_sale' (did you mean: total_sales",
        ),
        (
            Query(dimensions=("item.brand",)),
            Code.E_NAME_NOT_FOUND,
            "unknown field 'item.brand' (did you mean: item.i_brand",
        ),
        (
            Query(dimensions=("items.i_brand",)),
            Code.E_NAME_NOT_FOUND,
            "unknown dataset 'items' (did you mean: item",
        ),
        (
            Query(dimensions=("i_brand",)),
            Code.E_NAME_NOT_FOUND,
            "must be dataset.field, got 'i_brand' (did you mean: item.i_brand",
        ),
        (Query(), Code.E_EMPTY_AGGREGATION_QUERY, "at least one measure or dimension"),
        (
            Query(fields=("store_sales.ss_quantity",)),
            Code.UNSUPPORTED_QUERY,
            "scalar queries (fields) are not supported yet",
        ),
        (
            Query(measures=("total_sales",), fields=("store_sales.ss_quantity",)),
            Code.E_MIXED_QUERY_SHAPE,
            "either fields",
        ),
        (
            Query(measures=("SUM(store_sales.ss_quantity)",)),
            Code.UNSUPPORTED_QUERY,
            "measures are metric names; ad-hoc aggregates",
        ),
        (
            Query(dimensions=("item.i_brand AS brand",)),
            Code.UNSUPPORTED_QUERY,
            "aliases and expressions are not supported yet",
        ),
        (
            Query(measures=("total_sales",), where=("total_sale > 1",)),
            Code.E_NAME_NOT_FOUND,
            "unqualified column 'total_sale' in 'total_sale > 1'; use dataset.field or a "
            "metric name (did you mean: total_sales",
        ),
        (
            Query(measures=("total_sales",), where=("ss_quantity > 1",)),
            Code.E_NAME_NOT_FOUND,
            "use dataset.field or a metric name (did you mean: store_sales.ss_quantity",
        ),
        (
            Query(
                measures=("total_sales",),
                dimensions=("item.i_brand", "store.s_store_sk"),
                where=("brand_rank_in_store <= 3",),
            ),
            Code.E_WINDOW_IN_WHERE,
            "where 'brand_rank_in_store <= 3' uses a window function",
        ),
        (
            Query(measures=("total_sales",), where=("RANK() OVER (ORDER BY COUNT(*)) < 3",)),
            Code.E_WINDOW_IN_WHERE,
            "uses a window function",
        ),
        (
            Query(
                measures=("total_sales",),
                dimensions=("item.i_brand", "store.s_store_sk"),
                having=("brand_rank_in_store <= 3",),
            ),
            Code.UNSUPPORTED_QUERY,
            "cannot filter a window metric in the same SELECT",
        ),
        (
            Query(measures=("total_sales",), where=("total_sales > 1",)),
            Code.E_AGGREGATE_IN_WHERE,
            "where 'total_sales > 1' uses an aggregate",
        ),
        (
            Query(measures=("total_sales",), where=("COUNT(*) > 10",)),
            Code.E_AGGREGATE_IN_WHERE,
            "filter aggregated rows in having",
        ),
        (
            Query(
                measures=("total_sales",),
                dimensions=("item.i_brand",),
                having=("total_sales > store_sales.ss_quantity",),
            ),
            Code.E_MIXED_PREDICATE_LEVEL,
            "mixes an aggregate with store_sales.ss_quantity, a row-level column that is not "
            "a dimension of this query",
        ),
        (
            Query(
                measures=("total_sales",),
                dimensions=("customer.customer_full_name",),
                having=("total_sales > 1 AND customer.c_first_name <> ''",),
            ),
            Code.E_MIXED_PREDICATE_LEVEL,
            "mixes an aggregate with customer.c_first_name",
        ),
        (
            Query(
                measures=("total_sales",),
                dimensions=("item.i_brand",),
                where=("item.i_brand <> '' AND total_sales > 1",),
            ),
            Code.E_MIXED_PREDICATE_LEVEL,
            "mixes an aggregate with item.i_brand, a row-level column;",
        ),
        (
            Query(
                measures=("total_sales",),
                dimensions=("item.i_brand",),
                having=("store_sales.ss_quantity > 1",),
            ),
            Code.E_NON_AGGREGATE_IN_HAVING,
            "has no aggregate and reads store_sales.ss_quantity, which is not a dimension",
        ),
        (
            Query(measures=("total_sales",), where=("store_sales.ss_quantity IN (SELECT 1)",)),
            Code.QUERY_INVALID,
            "not allowed",
        ),
        # Both joined from store_sales, which the query does not read: no root.
        (
            Query(dimensions=("item.i_brand", "store.s_state")),
            Code.UNSUPPORTED_QUERY,
            "no dataset among ['item', 'store'] joins all the others",
        ),
        (
            Query(measures=("total_sales",), where=("date_dim.d_year = ",)),
            Code.QUERY_INVALID,
            "cannot parse",
        ),
    ],
)
def test_errors(query, code, message):
    with pytest.raises(PlanError) as e:
        P.sql(query)
    assert e.value.code == code
    assert message in str(e.value)


def test_unknown_names_come_with_suggestions():
    with pytest.raises(PlanError) as e:
        P.sql(Query(measures=("total_sale",)))
    assert e.value.suggestions[0] == Suggestion("name", "total_sales")
    with pytest.raises(PlanError) as e:
        P.sql(Query(measures=("zzz",)))
    assert e.value.suggestions == () and str(e.value) == "unknown metric 'zzz'"


@pytest.mark.parametrize(
    "f",
    [
        "1 = 1; DROP TABLE tpcds.item",
        "date_dim.d_year = 1 UNION ALL SELECT 1",
        "date_dim.d_year = 1 SETTINGS max_threads = 1",
        "date_dim.d_year = {y:UInt16}",
        "date_dim.d_year IN (SELECT 1)",
        "EXISTS (SELECT 1)",
    ],
)
def test_filters_cannot_smuggle_statements(f):
    """Filters come from an agent as free text: expressions only, never statements."""
    with pytest.raises(PlanError):
        P.sql(Query(measures=("total_sales",), where=(f,)))


def test_filters_pass_functions_through():
    # Any function goes to ClickHouse as written; grants and quotas there are the boundary.
    sql = P.sql(Query(measures=("total_sales",), where=("toString(date_dim.d_year) = '1998'",)))
    assert "WHERE toString(date_dim.d_year) = '1998'" in sql


def test_having():
    sql = P.sql(
        Query(
            measures=("total_sales",),
            dimensions=("item.i_brand",),
            where="date_dim.d_year = 1998",
            having=(
                "total_sales > 1000000",  # metric by name
                "COUNT(*) > 10",  # raw aggregate
            ),
        )
    )
    assert sql.endswith(
        "WHERE date_dim.d_year = 1998 GROUP BY item.i_brand "
        "HAVING SUM(store_sales.ss_ext_sales_price) > 1000000 AND COUNT(*) > 10 "
        "ORDER BY total_sales DESC SETTINGS join_use_nulls = 1"
    )
    # No dimensions: HAVING without GROUP BY is still one SELECT.
    assert P.sql(Query(measures=("total_sales",), having=("total_sales > 1",))).endswith(
        "HAVING SUM(store_sales.ss_ext_sales_price) > 1 SETTINGS join_use_nulls = 1"
    )
    # A HAVING filter may use a dimension of the query, even one with an expression.
    sql = P.sql(
        Query(
            measures=("total_sales",),
            dimensions=("customer.customer_full_name",),
            having=("total_sales > 1 AND customer.customer_full_name <> ''",),
        )
    )
    assert "HAVING (SUM(store_sales.ss_ext_sales_price) > 1) AND ((customer.c_first_name" in sql
    # Only dimensions, no aggregate: legal in having, as in SQL (#246 §6.3).
    sql = P.sql(
        Query(measures=("total_sales",), dimensions=("item.i_brand",), having="item.i_brand <> ''")
    )
    assert "GROUP BY item.i_brand HAVING item.i_brand <> '' ORDER BY" in sql


def test_filter_on_unselected_metric_adds_its_join():
    sql = P.sql(
        Query(
            measures=("total_sales",),
            dimensions=("item.i_brand",),
            having=("customer_lifetime_value > 1",),
        )
    )
    assert (
        "LEFT JOIN tpcds.customer AS customer "
        "ON store_sales.ss_customer_sk = customer.c_customer_sk" in sql
    )
    assert sql.endswith(
        "GROUP BY item.i_brand HAVING (SUM(store_sales.ss_ext_sales_price) / "
        "COUNT(DISTINCT customer.c_customer_sk)) > 1 ORDER BY total_sales DESC "
        "SETTINGS join_use_nulls = 1"
    )


def _variant(mutate) -> Planner:
    """A planner over a copy of the fixture model changed by ``mutate(data)``."""
    import copy

    from ossie import OssieDocument

    m = copy.deepcopy(MODEL.model_dump(by_alias=True))
    mutate(m)
    return Planner(OssieDocument.model_validate(m))


def test_join_requires_unique_key_on_target():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        item["primary_key"] = ["i_item_id"]
        item["unique_keys"] = None

    with pytest.raises(PlanError, match="do not cover a primary or unique key") as e:
        _variant(mutate).sql(Query(measures=("total_sales",), dimensions=("item.i_brand",)))
    assert e.value.code == Code.UNSUPPORTED_QUERY


def test_join_requires_declared_key_on_target():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        item["primary_key"] = None
        item["unique_keys"] = None

    with pytest.raises(PlanError, match="'item' declares no primary_key or unique_keys") as e:
        _variant(mutate).sql(Query(measures=("total_sales",), dimensions=("item.i_brand",)))
    assert e.value.code == Code.E_PRIMARY_KEY_REQUIRED


def _superset_of_item_key(m):
    """Join store_sales to item on (i_item_sk, i_manufact_id): wider than the key."""
    r = next(r for r in m["relationships"] if r["name"] == "store_sales_to_item")
    r["from_columns"] = ["ss_item_sk", "ss_store_sk"]
    r["to_columns"] = ["I_ITEM_SK", "i_manufact_id"]


def test_join_on_superset_of_key():
    """A superset of a unique key is unique too (apache/ossie#330 reads it the same way)."""
    sql = _variant(_superset_of_item_key).sql(
        Query(measures=("total_sales",), dimensions=("item.i_brand",))
    )
    assert (
        "LEFT JOIN tpcds.item AS item ON store_sales.ss_item_sk = item.I_ITEM_SK "
        "AND store_sales.ss_store_sk = item.i_manufact_id"
    ) in sql


def test_join_on_superset_of_unique_key():
    def mutate(m):
        _superset_of_item_key(m)
        item = next(d for d in m["datasets"] if d["name"] == "item")
        item["primary_key"] = ["i_item_id"]  # not covered; the unique key is
        item["unique_keys"] = [["i_item_sk"]]

    sql = _variant(mutate).sql(Query(measures=("total_sales",), dimensions=("item.i_brand",)))
    assert "LEFT JOIN tpcds.item AS item ON" in sql


def test_dictionary_joined_on_superset_of_its_key_is_a_join():
    """dictGet by the key alone would ignore the extra column and match rows the join would not."""
    planner = _variant(_superset_of_item_key)
    planner.catalog = {"item": TableInfo("Dictionary", frozenset(), dictionary_key="i_item_sk")}
    sql = planner.sql(Query(measures=("total_sales",), dimensions=("item.i_brand",)))
    assert "LEFT JOIN tpcds.item AS item" in sql and "dictGet" not in sql


def test_anonymous_aggregate_counts_as_one():
    # APPROX_PERCENTILE has no SQLGlot node; it must still count as an aggregate.
    sql = P.sql(
        Query(
            measures=("total_sales",),
            dimensions=("item.i_brand",),
            having=("APPROX_PERCENTILE(store_sales.ss_net_profit, 0.95) > 10",),
        )
    )
    assert "HAVING quantileTDigest(0.95)(store_sales.ss_net_profit) > 10" in sql
    with pytest.raises(PlanError) as e:
        P.sql(
            Query(
                measures=("total_sales",),
                where=("APPROX_PERCENTILE(store_sales.ss_net_profit, 0.95) > 10",),
            )
        )
    assert e.value.code == Code.E_AGGREGATE_IN_WHERE


def test_selected_names_must_differ():
    with pytest.raises(PlanError, match="two selected columns named 'total_sales'") as e:
        P.sql(Query(measures=("total_sales", "TOTAL_SALES")))
    assert e.value.code == Code.UNSUPPORTED_QUERY

    def mutate(m):
        next(x for x in m["metrics"] if x["name"] == "total_sales")["name"] = "I_BRAND"

    with pytest.raises(PlanError, match="two selected columns named 'I_BRAND'"):
        _variant(mutate).sql(Query(measures=("i_brand",), dimensions=("item.i_brand",)))

    def same_field_name(m):
        next(d for d in m["datasets"] if d["name"] == "store")["fields"][0]["name"] = "i_brand"

    with pytest.raises(PlanError, match="fields of two datasets, cannot share a name"):
        _variant(same_field_name).sql(Query(dimensions=("item.i_brand", "store.i_brand")))


def test_two_roots_are_ambiguous():
    def mutate(m):
        m["relationships"] += [
            {"name": "i2s", "from": "item", "to": "store",
             "from_columns": ["i_item_sk"], "to_columns": ["s_store_sk"]},
            {"name": "s2i", "from": "store", "to": "item",
             "from_columns": ["s_store_sk"], "to_columns": ["i_item_sk"]},
        ]  # fmt: skip

    with pytest.raises(PlanError, match="ambiguous root dataset among \\['item', 'store'\\]") as e:
        _variant(mutate).sql(Query(dimensions=("item.i_brand", "store.s_state")))
    assert e.value.code == Code.UNSUPPORTED_QUERY


def test_field_may_only_use_its_own_columns():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        brand = next(f for f in item["fields"] if f["name"] == "i_brand")
        brand["expression"]["dialects"][0]["expression"] = "store.s_state"

    with pytest.raises(PlanError, match="field item.i_brand references another dataset") as e:
        _variant(mutate).sql(Query(dimensions=("item.i_brand",)))
    assert e.value.code == Code.UNSUPPORTED_QUERY


def test_catalog_final_and_dictionary():
    """The executor's catalog changes the SQL shape; pinned here without a server."""
    catalog = {
        "store_sales": TableInfo("ReplacingMergeTree", frozenset(), dedup=True),
        "item": TableInfo("Dictionary", frozenset(), dictionary_key="i_item_sk"),
    }
    sql = Planner(MODEL, catalog).sql(
        Query(measures=("total_sales",), dimensions=("item.i_brand", "item.i_item_sk"))
    )
    assert sql == (
        "SELECT dictGetOrNull('tpcds.item', 'i_brand', store_sales.ss_item_sk) AS i_brand, "
        "CASE WHEN dictHas('tpcds.item', store_sales.ss_item_sk) THEN store_sales.ss_item_sk "
        "ELSE NULL END AS i_item_sk, SUM(store_sales.ss_ext_sales_price) AS total_sales "
        "FROM tpcds.store_sales AS store_sales FINAL "
        "GROUP BY dictGetOrNull('tpcds.item', 'i_brand', store_sales.ss_item_sk), "
        "CASE WHEN dictHas('tpcds.item', store_sales.ss_item_sk) THEN store_sales.ss_item_sk "
        "ELSE NULL END ORDER BY total_sales DESC SETTINGS join_use_nulls = 1"
    )
    # A dictionary joined on something other than its key is a plain LEFT JOIN.
    catalog["item"] = TableInfo("Dictionary", frozenset(), dictionary_key="i_item_id")
    sql = Planner(MODEL, catalog).sql(
        Query(measures=("total_sales",), dimensions=("item.i_brand",))
    )
    assert "LEFT JOIN tpcds.item AS item" in sql and "dictGet" not in sql


def test_two_relationships_to_the_same_dataset_are_ambiguous():
    def mutate(m):
        sold = next(r for r in m["relationships"] if r["name"] == "store_sales_to_date")
        m["relationships"].append(sold | {"name": "store_sales_to_ship_date"})

    with pytest.raises(PlanError, match=r"ambiguous join .* 'store_sales_to_ship_date'") as e:
        _variant(mutate).sql(Query(measures=("total_sales",), dimensions=("date_dim.d_year",)))
    assert e.value.code == Code.E_AMBIGUOUS_PATH


def _apart(m):
    """Copies of datasets: store_area, joined from store only; lonely, joined to nothing;
    sales_copy, a second fact that shares item with store_sales."""
    store = next(d for d in m["datasets"] if d["name"] == "store")
    item = next(d for d in m["datasets"] if d["name"] == "item")
    sales = next(d for d in m["datasets"] if d["name"] == "store_sales")
    m["datasets"] += [
        {**store, "name": "store_area"},
        {**item, "name": "lonely"},
        {**sales, "name": "sales_copy"},
    ]
    m["relationships"] += [
        {"name": "store_to_area", "from": "store", "to": "store_area",
         "from_columns": ["s_store_sk"], "to_columns": ["s_store_sk"]},
        {"name": "copy_to_item", "from": "sales_copy", "to": "item",
         "from_columns": ["ss_item_sk"], "to_columns": ["i_item_sk"]},
    ]  # fmt: skip
    for name, e in (
        ("price_gap", "MAX(store_sales.ss_sales_price) - MAX(lonely.i_current_price)"),
        ("lonely_items", "COUNT(lonely.i_item_sk)"),
        ("copy_sales", "SUM(sales_copy.ss_ext_sales_price)"),
    ):
        m["metrics"].append(
            {"name": name, "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": e}]}}
        )


@pytest.mark.parametrize(
    ("query", "code", "message"),
    [
        (
            Query(measures=("total_sales",), dimensions=("lonely.i_brand",)),
            Code.E_NO_PATH,
            "no relationship path connects store_sales with lonely",
        ),
        (
            Query(measures=("total_sales",), where=("lonely.i_brand = 'x'",)),
            Code.E_NO_PATH,
            "no relationship path connects store_sales with lonely",
        ),
        (
            Query(measures=("price_gap",)),
            Code.E3013_NO_STITCHING_DIMENSION,
            "metric 'price_gap' reads lonely and store_sales, which share no dimension",
        ),
        # Two facts with no dimension reachable from both: E3013, with dimensions or not.
        (
            Query(measures=("total_sales", "lonely_items")),
            Code.E3013_NO_STITCHING_DIMENSION,
            "metric 'total_sales' and metric 'lonely_items' read store_sales and lonely, "
            "which share no dimension",
        ),
        (
            Query(measures=("total_sales", "lonely_items"), dimensions=("item.i_brand",)),
            Code.E3013_NO_STITCHING_DIMENSION,
            "which share no dimension",
        ),
        # Two facts sharing item: #246 stitches them (§6.8.2), this version cannot.
        (
            Query(measures=("total_sales", "copy_sales"), dimensions=("item.i_brand",)),
            Code.UNSUPPORTED_QUERY,
            "each would have to be aggregated on its own",
        ),
        (
            Query(measures=("total_sales", "copy_sales")),
            Code.UNSUPPORTED_QUERY,
            "each would have to be aggregated on its own",
        ),
        # A path, but of two relationships: #246 answers it, this version does not.
        (
            Query(measures=("total_sales",), dimensions=("store_area.s_state",)),
            Code.UNSUPPORTED_QUERY,
            "joined from 'store_sales' only through more than one relationship",
        ),
    ],
)
def test_datasets_without_a_direct_join(query, code, message):
    with pytest.raises(PlanError) as e:
        _variant(_apart).sql(query)
    assert e.value.code == code and message in str(e.value)


def test_cli(capsys):
    assert main(["sql", str(FIXTURE), "-m", "total_sales", "-d", "item.i_brand", "-l", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SELECT\n") and "LEFT JOIN tpcds.item" in out
    assert main(["sql", str(FIXTURE), "-m", "nope"]) == 1
    assert capsys.readouterr().err == "error: E_NAME_NOT_FOUND: unknown metric 'nope'\n"
    args = ["sql", str(FIXTURE), "-m", "total_sales", "-d", "item.i_brand"]
    assert main([*args, "-w", "date_dim.d_year = 1998", "--having", "total_sales > 1"]) == 0
    out = capsys.readouterr().out
    assert "WHERE\n  date_dim.d_year = 1998" in out and "HAVING\n  SUM(" in out
    assert main([*args, "-o", "total_sales desc", "-o", "i_brand nulls first"]) == 0
    assert "total_sales DESC NULLS FIRST,\n  i_brand ASC NULLS FIRST" in capsys.readouterr().out
    assert main([*args, "-o", "total_sales down"]) == 1
    assert capsys.readouterr().err.startswith("error: QUERY_INVALID: order_by item")


def test_cli_reports_connection_and_policy_errors_without_traceback(capsys):
    nowhere = "http://127.0.0.1:9"  # nothing listens; a refused connection is an error line
    assert main(["sql", str(FIXTURE), "-m", "total_sales", "--url", nowhere]) == 1
    assert capsys.readouterr().err.startswith("error: ")
    args = ["sql", str(FIXTURE), "-m", "total_sales", "--url", nowhere, "--policy", "nope.yaml"]
    assert main(args) == 1
    assert "nope.yaml" in capsys.readouterr().err


# --- execution against TPC-DS (fixture ``tpcds`` in conftest) -----------------


@pytest.mark.parametrize(
    "ref",
    [f"{d.name}.{f.name}" for d in MODEL.datasets for f in d.fields],
)
def test_every_field_is_a_dimension(ref, tpcds):
    assert tpcds.query(P.sql(Query(dimensions=(ref,), limit=1))).result_rows


def test_no_fan_out_and_no_loss(tpcds):
    """Grouping by a joined dimension must neither multiply nor drop fact rows."""
    total = tpcds.query("SELECT SUM(ss_ext_sales_price) FROM tpcds.store_sales").result_rows[0][0]
    grouped = tpcds.query(
        P.sql(Query(measures=("total_sales",), dimensions=("item.i_brand", "customer.c_last_name")))
    ).result_rows
    assert sum(r[-1] for r in grouped if r[-1] is not None) == total


def test_filter_and_values(tpcds):
    direct = tpcds.query(
        "SELECT SUM(ss.ss_ext_sales_price) FROM tpcds.store_sales ss "
        "JOIN tpcds.date_dim d ON ss.ss_sold_date_sk = d.d_date_sk WHERE d.d_year = 1998"
    ).result_rows[0][0]
    planned = tpcds.query(
        P.sql(Query(measures=("total_sales",), where=("date_dim.d_year = 1998",)))
    ).result_rows[0][0]
    assert planned == direct


def test_having_values(tpcds):
    direct = tpcds.query(
        "SELECT i.i_brand, SUM(ss.ss_ext_sales_price) AS s FROM tpcds.store_sales ss "
        "LEFT JOIN tpcds.item i ON ss.ss_item_sk = i.i_item_sk GROUP BY i.i_brand "
        "HAVING s > 100000 ORDER BY i.i_brand NULLS LAST SETTINGS join_use_nulls = 1"
    ).result_rows
    planned = tpcds.query(
        P.sql(
            Query(
                measures=("total_sales",),
                dimensions=("item.i_brand",),
                having=("total_sales > 100000",),
                order_by=(Order("item.i_brand"),),
            )
        )
    ).result_rows
    assert planned and planned == direct


ANSWERABLE = [m for m in MODEL.metrics if m.name not in unanswerable_metrics(MODEL)]


@pytest.mark.parametrize("metric", ANSWERABLE, ids=[m.name for m in ANSWERABLE])
def test_every_metric_executes(metric, tpcds):
    dims = {
        "cumulative_sales": ("date_dim.d_date",),
        "brand_rank_in_store": ("store.s_store_sk", "item.i_brand"),
        "monthly_sales_change": ("date_dim.d_year", "date_dim.d_moy"),
    }.get(metric.name, ("store.s_state",))
    rows = tpcds.query(P.sql(Query(measures=(metric.name,), dimensions=dims, limit=5))).result_rows
    assert rows


def _with_metrics(**expressions: str) -> Planner:
    """The fixture with extra metrics and store_extra, a one-to-one extension of store."""

    def mutate(m):
        store = next(d for d in m["datasets"] if d["name"] == "store")
        m["datasets"].append({**store, "name": "store_extra"})
        m["relationships"].append(
            {
                "name": "store_to_extra",
                "from": "store",
                "to": "store_extra",
                "from_columns": ["s_store_sk"],
                "to_columns": ["s_store_sk"],
            }
        )
        m["relationships"].append(
            {
                "name": "sales_to_extra",
                "from": "store_sales",
                "to": "store_extra",
                "from_columns": ["ss_store_sk"],
                "to_columns": ["s_store_sk"],
            }
        )
        for name, e in expressions.items():
            m["metrics"].append(
                {
                    "name": name,
                    "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": e}]},
                }
            )

    return _variant(mutate)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        # A joined dataset's rows repeat once per root row: store_productivity sums
        # each store's employees once per sale.
        (
            Query(measures=("store_productivity",), dimensions=("store.s_store_name",)),
            "metric 'store_productivity' applies SUM(store.s_number_employees) to store "
            "columns alone over store_sales rows",
        ),
        (
            Query(measures=("total_sales",), having=("store_productivity > 1",)),
            "having 'store_productivity > 1' applies SUM(store.s_number_employees)",
        ),
        (
            Query(measures=("total_sales", "headcount"), dimensions=("store.s_state",)),
            "metric 'headcount' aggregates store rows, but this question is answered over "
            "store_sales rows (because of metric 'total_sales')",
        ),
        # Even a repeat-safe aggregate is about other rows: only customers who bought.
        (
            Query(measures=("customers",), where=("store_sales.ss_quantity > 1",)),
            "(because of where 'store_sales.ss_quantity > 1')",
        ),
        (
            Query(measures=("customers",), dimensions=("store_sales.ss_store_sk",)),
            "(because of dimension 'store_sales.ss_store_sk')",
        ),
        # Checked after rewrite: APPROX_PERCENTILE is an aggregate only then.
        # Nested in another rewrite: the check sees the inner one rewritten too.
        (
            Query(measures=("sales_per_nonzero_median_staff",)),
            "applies quantileTDigest(0.5)(store.s_number_employees)",
        ),
        # COUNT(*) alone counts whatever rows the question is about: stores here,
        # sales next to total_sales.
        (
            Query(measures=("cnt",), dimensions=("store.s_state",)),
            "metric 'cnt' uses COUNT(*), which reads no column",
        ),
        (
            Query(measures=("total_sales",), having=("cnt > 1",)),
            "metric 'cnt' uses COUNT(*), which reads no column",
        ),
        # A FILTER column counts for its aggregate: this counts sales once per store.
        (
            Query(measures=("total_sales", "tn_stores")),
            "metric 'tn_stores' aggregates store rows",
        ),
        # Only the root's key makes a join one-to-one: sales repeat per store_extra row.
        (
            Query(measures=("extra_staff",), dimensions=("store.s_state",)),
            "metric 'extra_staff' applies SUM(store_extra.s_number_employees)",
        ),
        (
            Query(measures=("sales_per_median_staff",)),
            "metric 'sales_per_median_staff' applies "
            "quantileTDigest(0.5)(store.s_number_employees)",
        ),
    ],
)
def test_refuses_aggregates_over_other_rows_than_the_root(query, message):
    planner = _with_metrics(
        headcount="SUM(store.s_number_employees)",
        customers="COUNT(DISTINCT customer.c_customer_sk)",
        sales_per_median_staff="SUM(store_sales.ss_ext_sales_price) / "
        "APPROX_PERCENTILE(store.s_number_employees, 0.5)",
        sales_per_nonzero_median_staff="SUM(store_sales.ss_ext_sales_price) / "
        "NULLIFZERO(APPROX_PERCENTILE(store.s_number_employees, 0.5))",
        cnt="COUNT(*)",
        tn_stores="COUNT(*) FILTER (WHERE store.s_state = 'TN')",
        extra_staff="SUM(store_sales.ss_ext_sales_price) / SUM(store_extra.s_number_employees)",
    )
    with pytest.raises(PlanError) as e:
        planner.sql(query)
    assert message in str(e.value) and e.value.code == Code.UNSUPPORTED_QUERY


def test_model_names_win_over_the_expression_check():
    # A metric named like no identifier is still a name of the model.
    planner = _with_metrics(**{"gross-margin": "SUM(store_sales.ss_net_profit)", "const": "1"})
    sql = planner.sql(Query(measures=("Gross-Margin",), dimensions=("item.i_brand",)))
    assert 'AS "gross-margin"' in sql and 'ORDER BY "gross-margin" DESC' in sql
    # A query that reads no dataset is refused, not a crash.
    with pytest.raises(PlanError) as e:
        planner.sql(Query(measures=("const",)))
    assert "reads no column of any dataset" in str(e.value)
    assert e.value.code == Code.UNSUPPORTED_QUERY


_SPLIT = ". Ask for each measure in its own question"


@pytest.mark.parametrize(
    ("query", "hint"),
    [
        # Each metric plans alone by the dimension: match the rows of the first answer.
        (
            Query(measures=("total_sales", "headcount"), dimensions=("store.s_state",)),
            _SPLIT + "; for the others over the rows of the first answer, such as its top 5, "
            'filter them with where "store.s_state IN (...)" listing the values it returned, '
            'or with "store.s_state IN (...) OR store.s_state IS NULL" if one is NULL',
        ),
        # Datasets not joined from one root, no dimension: separate questions answer it.
        (Query(measures=("headcount", "items")), _SPLIT),
        # One IN list cannot pick the (state, city) pairs of the first answer.
        (
            Query(
                measures=("total_sales", "headcount"),
                dimensions=("store.s_state", "store.s_city"),
            ),
            _SPLIT,
        ),
        # headcount cannot be grouped by item either: no split answers it.
        (Query(measures=("headcount", "items"), dimensions=("item.i_brand",)), None),
        # The dimension, not the mix of metrics, is the cause.
        (Query(measures=("headcount", "stores"), dimensions=("store_sales.ss_quantity",)), None),
    ],
)
def test_refusal_advises_a_split_only_when_it_answers(query, hint):
    planner = _with_metrics(
        headcount="SUM(store.s_number_employees)",
        stores="COUNT(store.s_store_sk)",
        items="COUNT(item.i_item_sk)",
    )
    with pytest.raises(PlanError) as e:
        planner.sql(query)
    if hint is None:
        assert _SPLIT not in str(e.value)
        assert all(s.kind != "query" for s in e.value.suggestions)
    else:
        assert str(e.value).endswith(hint)
        assert e.value.suggestions[-1] == Suggestion("query", hint.removeprefix(". "))
    # store and item reach no dataset in common: unrelated facts, whatever the split.
    unrelated = set(query.measures) == {"headcount", "items"}
    assert (e.value.code == Code.E3013_NO_STITCHING_DIMENSION) is unrelated


@pytest.mark.parametrize(
    ("measures", "dimensions"),
    [
        (("customer_lifetime_value",), ("store.s_state",)),  # customer only under DISTINCT
        (("cumulative_sales",), ("date_dim.d_date",)),  # date_dim only in OVER
        (("brand_rank_in_store",), ("store.s_store_sk", "item.i_brand")),
        (("monthly_sales_change",), ("date_dim.d_year", "date_dim.d_moy")),
        (("revenue_at_list_price",), ("store.s_state",)),  # one value per root row
        (("sales_per_largest_staff",), ("item.i_brand",)),  # MAX ignores repeats
        (("sales_per_median_distinct_staff",), ("item.i_brand",)),  # so does DISTINCT
        # COUNT(*) next to an aggregate that names its dataset counts that dataset's rows.
        (("average_sale",), ("store.s_state",)),
        (("bulk_sales",), ("store.s_state",)),  # FILTER reads the root
        (("bulk_median_quantity",), ("store.s_state",)),  # FILTER under a rewrite too
        (("staff_per_extra",), ("store.s_state",)),  # store_extra rows do not repeat
    ],
)
def test_allows_aggregates_that_read_root_rows(measures, dimensions):
    planner = _with_metrics(
        revenue_at_list_price="SUM(store_sales.ss_quantity * item.i_current_price)",
        sales_per_largest_staff="SUM(store_sales.ss_ext_sales_price) / "
        "MAX(store.s_number_employees)",
        sales_per_median_distinct_staff="SUM(store_sales.ss_ext_sales_price) / "
        "MEDIAN(DISTINCT store.s_number_employees)",
        average_sale="SUM(store_sales.ss_ext_sales_price) / COUNT(*)",
        bulk_sales="COUNT(*) FILTER (WHERE store_sales.ss_quantity > 50)",
        bulk_median_quantity="PERCENTILE_DISC(0.5) WITHIN GROUP "
        "(ORDER BY store_sales.ss_quantity) FILTER (WHERE store_sales.ss_quantity > 50)",
        staff_per_extra="SUM(store.s_number_employees) / SUM(store_extra.s_number_employees)",
    )
    planner.sql(Query(measures=measures, dimensions=dimensions))


def test_unanswerable_metrics():
    assert list(unanswerable_metrics(MODEL)) == ["store_productivity"]
    # Dataset names are case-insensitive: one dataset, spelled two ways.
    planner = _with_metrics(
        margin="SUM(Store_Sales.ss_ext_sales_price) - SUM(store_sales.ss_net_profit)"
    )
    assert "margin" not in unanswerable_metrics(planner.model)
    assert "SUM(store.s_number_employees)" in unanswerable_metrics(MODEL)["store_productivity"]
    planner = _with_metrics(
        typo="SUM(store.s_number_employes)",
        cnt="COUNT(*)",
        one_to_one="SUM(store.s_number_employees) / SUM(store_extra.s_number_employees)",
    )
    found = unanswerable_metrics(planner.model)
    assert "unknown field 'store.s_number_employes'" in found["typo"]
    assert "uses COUNT(*), which reads no column" in found["cnt"]
    assert "one_to_one" not in found


def test_unanswerable_metrics_are_hidden(tpcds):
    from dactopus_ossie_clickhouse.executor import Executor

    names = [m.name for m in Executor(tpcds, MODEL).model.metrics]
    assert "store_productivity" not in names and "total_sales" in names


def test_nested_rewrite_in_a_metric():
    planner = _with_metrics(
        median_or_null="NULLIFZERO(APPROX_PERCENTILE(store_sales.ss_quantity, 0.5))"
    )
    sql = planner.sql(Query(measures=("median_or_null",)))
    assert "nullIf(quantileTDigest(0.5)(store_sales.ss_quantity), 0) AS median_or_null" in sql
