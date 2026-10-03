import pytest

from ossie_clickhouse import load_model
from ossie_clickhouse.cli import main
from ossie_clickhouse.planner import PlanError, Planner, Query, TableInfo, source_table
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)
P = Planner(MODEL)


def test_source_table():
    assert source_table("tpcds.public.store_sales").sql() == "tpcds.store_sales"
    assert source_table("db.t").sql() == "db.t"
    assert source_table("t").sql() == "t"
    with pytest.raises(PlanError, match="query sources"):
        source_table("SELECT * FROM t")
    with pytest.raises(PlanError, match="cannot map"):
        source_table("a.b.c.d")


def test_single_dataset_no_join():
    sql = P.sql(Query(metrics=("total_sales",)))
    assert sql == (
        "SELECT SUM(store_sales.ss_ext_sales_price) AS total_sales "
        "FROM tpcds.store_sales AS store_sales SETTINGS join_use_nulls = 1"
    )


def test_dimension_filter_join_snapshot():
    sql = P.sql(
        Query(
            metrics=("total_sales", "total_profit"),
            dimensions=("item.i_brand",),
            filters=("date_dim.d_year = 1998",),
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
    sql = P.sql(Query(dimensions=("customer.customer_full_name",), metrics=("total_sales",)))
    expr = "customer.c_first_name || ' ' || customer.c_last_name"
    assert f"{expr} AS customer_full_name" in sql and f"GROUP BY {expr}" in sql


def test_case_insensitive_resolution_keeps_physical_case():
    sql = P.sql(Query(metrics=("TOTAL_SALES",), dimensions=("ITEM.I_BRAND",)))
    assert "item.i_brand AS i_brand" in sql and "SUM(store_sales.ss_ext_sales_price)" in sql


def test_dimension_only_query():
    sql = P.sql(Query(dimensions=("store.s_state",)))
    assert sql.startswith("SELECT store.s_state AS s_state FROM tpcds.store AS store GROUP BY")


def test_window_metric_passes_through():
    sql = P.sql(Query(metrics=("cumulative_sales",), dimensions=("date_dim.d_date",)))
    assert "SUM(SUM(store_sales.ss_ext_sales_price)) OVER (ORDER BY date_dim.d_date" in sql


def test_order_by():
    # Default: first metric descending, only when there are rows to rank.
    q = Query(metrics=("total_profit", "total_sales"), dimensions=("item.i_brand",))
    assert "ORDER BY total_profit DESC" in P.sql(q)
    assert "ORDER BY" not in P.sql(Query(metrics=("total_sales",)))
    assert "ORDER BY" not in P.sql(Query(dimensions=("item.i_brand",)))
    q = Query(
        metrics=("total_sales",), dimensions=("item.i_brand",), order_by=("total_sales desc",)
    )
    assert P.sql(q).endswith(
        "GROUP BY item.i_brand ORDER BY total_sales DESC SETTINGS join_use_nulls = 1"
    )
    q = Query(
        metrics=("total_sales",),
        dimensions=("item.i_brand",),
        order_by=("item.i_brand", "TOTAL_SALES"),
    )
    # NULLs last both ways: ClickHouse's default, so no NULLS clause (docs/design.md).
    assert "ORDER BY i_brand ASC, total_sales ASC SETTINGS" in P.sql(q)
    # The caller's spelling of dataset.field never leaks into ORDER BY: aliases are the model's.
    q = Query(dimensions=("date_dim.D_YEAR",), order_by=("DATE_DIM.D_YEAR",))
    assert "AS d_year" in P.sql(q) and "ORDER BY d_year ASC" in P.sql(q)
    with pytest.raises(
        PlanError, match="not a selected metric or dimension \\(did you mean: total_sales"
    ):
        P.sql(Query(metrics=("total_sales",), order_by=("total_sale",)))
    with pytest.raises(PlanError, match="use 'name'"):
        P.sql(Query(metrics=("total_sales",), order_by=("total_sales down",)))
    # i_brand is selectable as "i_brand" and "item.i_brand"; the hint names it once.
    with pytest.raises(PlanError, match=r"\(did you mean: i_brand\?\)$"):
        P.sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",), order_by=("brand",)))


def test_deterministic():
    q = Query(metrics=("store_productivity",), dimensions=("store.s_state", "item.i_category"))
    assert P.sql(q) == P.sql(q)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (Query(metrics=("total_sale",)), "unknown metric 'total_sale' (did you mean: total_sales"),
        (
            Query(dimensions=("item.brand",)),
            "unknown field 'item.brand' (did you mean: item.i_brand",
        ),
        (Query(dimensions=("items.i_brand",)), "unknown dataset 'items' (did you mean: item"),
        (Query(dimensions=("i_brand",)), "must be dataset.field"),
        (Query(), "at least one metric or dimension"),
        (
            Query(metrics=("total_sales",), filters=("total_sale > 1",)),
            "unqualified column 'total_sale' in 'total_sale > 1'; use dataset.field or a "
            "metric name (did you mean: total_sales",
        ),
        (
            Query(metrics=("total_sales",), filters=("ss_quantity > 1",)),
            "use dataset.field or a metric name (did you mean: store_sales.ss_quantity",
        ),
        (
            Query(
                metrics=("total_sales",),
                dimensions=("item.i_brand", "store.s_store_sk"),
                filters=("brand_rank_in_store <= 3",),
            ),
            "'brand_rank_in_store <= 3' uses a window function",
        ),
        (
            Query(metrics=("total_sales",), filters=("RANK() OVER (ORDER BY COUNT(*)) < 3",)),
            "uses a window function",
        ),
        (
            Query(
                metrics=("total_sales",),
                dimensions=("item.i_brand",),
                filters=("total_sales > store_sales.ss_quantity",),
            ),
            "mixes an aggregate with store_sales.ss_quantity, which is not a dimension",
        ),
        (
            Query(
                metrics=("total_sales",),
                dimensions=("customer.customer_full_name",),
                filters=("total_sales > 1 AND customer.c_first_name <> ''",),
            ),
            "mixes an aggregate with customer.c_first_name",
        ),
        (
            Query(metrics=("total_sales",), filters=("store_sales.ss_quantity IN (SELECT 1)",)),
            "not allowed",
        ),
        (Query(dimensions=("item.i_brand", "store.s_state")), "not joined by direct relationships"),
        (Query(metrics=("total_sales",), filters=("date_dim.d_year = ",)), "cannot parse"),
    ],
)
def test_errors(query, message):
    with pytest.raises(PlanError, match=message.replace("(", r"\(").replace("?", r"\?")):
        P.sql(query)


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
        P.sql(Query(metrics=("total_sales",), filters=(f,)))


def test_filters_pass_functions_through():
    # Any function goes to ClickHouse as written; grants and quotas there are the boundary.
    sql = P.sql(Query(metrics=("total_sales",), filters=("toString(date_dim.d_year) = '1998'",)))
    assert "WHERE toString(date_dim.d_year) = '1998'" in sql


def test_aggregate_filters_go_to_having():
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("item.i_brand",),
            filters=(
                "date_dim.d_year = 1998",
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
    assert P.sql(Query(metrics=("total_sales",), filters=("total_sales > 1",))).endswith(
        "HAVING SUM(store_sales.ss_ext_sales_price) > 1 SETTINGS join_use_nulls = 1"
    )
    # A HAVING filter may use a dimension of the query, even one with an expression.
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("customer.customer_full_name",),
            filters=("total_sales > 1 AND customer.customer_full_name <> ''",),
        )
    )
    assert "HAVING SUM(store_sales.ss_ext_sales_price) > 1 AND customer.c_first_name" in sql


def test_filter_on_unselected_metric_adds_its_join():
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("item.i_brand",),
            filters=("store_productivity > 1",),
        )
    )
    assert "LEFT JOIN tpcds.store AS store ON store_sales.ss_store_sk = store.s_store_sk" in sql
    assert sql.endswith(
        "GROUP BY item.i_brand HAVING SUM(store_sales.ss_ext_sales_price) / "
        "nullIf(SUM(store.s_number_employees), 0) > 1 ORDER BY total_sales DESC "
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

    with pytest.raises(PlanError, match="do not cover a primary or unique key"):
        _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))


def test_join_requires_declared_key_on_target():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        item["primary_key"] = None
        item["unique_keys"] = None

    with pytest.raises(PlanError, match="'item' declares no primary_key or unique_keys"):
        _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))


def _superset_of_item_key(m):
    """Join store_sales to item on (i_item_sk, i_item_id): wider than the key."""
    r = next(r for r in m["relationships"] if r["name"] == "store_sales_to_item")
    r["from_columns"] = ["ss_item_sk", "ss_store_sk"]
    r["to_columns"] = ["I_ITEM_SK", "i_item_id"]


def test_join_on_superset_of_key():
    """A superset of a unique key is unique too (apache/ossie#330 reads it the same way)."""
    sql = _variant(_superset_of_item_key).sql(
        Query(metrics=("total_sales",), dimensions=("item.i_brand",))
    )
    assert (
        "LEFT JOIN tpcds.item AS item ON store_sales.ss_item_sk = item.I_ITEM_SK "
        "AND store_sales.ss_store_sk = item.i_item_id"
    ) in sql


def test_dictionary_joined_on_superset_of_its_key_is_a_join():
    """dictGet by the key alone would ignore the extra column and match rows the join would not."""
    planner = _variant(_superset_of_item_key)
    planner.catalog = {"item": TableInfo("Dictionary", frozenset(), dictionary_key="i_item_sk")}
    sql = planner.sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))
    assert "LEFT JOIN tpcds.item AS item" in sql and "dictGet" not in sql


def test_anonymous_aggregate_filter_goes_to_having():
    # APPROX_PERCENTILE has no SQLGlot node; it must still count as an aggregate.
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("item.i_brand",),
            filters=("APPROX_PERCENTILE(store_sales.ss_net_profit, 0.95) > 10",),
        )
    )
    assert "WHERE" not in sql
    assert "HAVING quantileTDigest(0.95)(store_sales.ss_net_profit) > 10" in sql


def test_selected_names_must_differ():
    with pytest.raises(PlanError, match="two selected columns named 'total_sales'"):
        P.sql(Query(metrics=("total_sales", "TOTAL_SALES")))

    def mutate(m):
        next(x for x in m["metrics"] if x["name"] == "total_sales")["name"] = "I_BRAND"

    with pytest.raises(PlanError, match="two selected columns named 'I_BRAND'"):
        _variant(mutate).sql(Query(metrics=("i_brand",), dimensions=("item.i_brand",)))

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

    with pytest.raises(PlanError, match="ambiguous root dataset among \\['item', 'store'\\]"):
        _variant(mutate).sql(Query(dimensions=("item.i_brand", "store.s_state")))


def test_field_may_only_use_its_own_columns():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        brand = next(f for f in item["fields"] if f["name"] == "i_brand")
        brand["expression"]["dialects"][0]["expression"] = "store.s_state"

    with pytest.raises(PlanError, match="field item.i_brand references another dataset"):
        _variant(mutate).sql(Query(dimensions=("item.i_brand",)))


def test_catalog_final_and_dictionary():
    """The executor's catalog changes the SQL shape; pinned here without a server."""
    catalog = {
        "store_sales": TableInfo("ReplacingMergeTree", frozenset(), dedup=True),
        "item": TableInfo("Dictionary", frozenset(), dictionary_key="i_item_sk"),
    }
    sql = Planner(MODEL, catalog).sql(
        Query(metrics=("total_sales",), dimensions=("item.i_brand", "item.i_item_sk"))
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
    sql = Planner(MODEL, catalog).sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))
    assert "LEFT JOIN tpcds.item AS item" in sql and "dictGet" not in sql


def test_two_relationships_to_the_same_dataset_are_ambiguous():
    def mutate(m):
        sold = next(r for r in m["relationships"] if r["name"] == "store_sales_to_date")
        m["relationships"].append(sold | {"name": "store_sales_to_ship_date"})

    with pytest.raises(PlanError, match=r"ambiguous join .* 'store_sales_to_ship_date'"):
        _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("date_dim.d_year",)))


def test_cli(capsys):
    assert main(["sql", str(FIXTURE), "-m", "total_sales", "-d", "item.i_brand", "-l", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SELECT\n") and "LEFT JOIN tpcds.item" in out
    assert main(["sql", str(FIXTURE), "-m", "nope"]) == 1
    assert "unknown metric" in capsys.readouterr().err


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
        P.sql(Query(metrics=("total_sales",), dimensions=("item.i_brand", "customer.c_last_name")))
    ).result_rows
    assert sum(r[-1] for r in grouped if r[-1] is not None) == total


def test_filter_and_values(tpcds):
    direct = tpcds.query(
        "SELECT SUM(ss.ss_ext_sales_price) FROM tpcds.store_sales ss "
        "JOIN tpcds.date_dim d ON ss.ss_sold_date_sk = d.d_date_sk WHERE d.d_year = 1998"
    ).result_rows[0][0]
    planned = tpcds.query(
        P.sql(Query(metrics=("total_sales",), filters=("date_dim.d_year = 1998",)))
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
                metrics=("total_sales",),
                dimensions=("item.i_brand",),
                filters=("total_sales > 100000",),
                order_by=("item.i_brand",),
            )
        )
    ).result_rows
    assert planned and planned == direct


@pytest.mark.parametrize("metric", MODEL.metrics, ids=[m.name for m in MODEL.metrics])
def test_every_metric_executes(metric, tpcds):
    dims = {
        "cumulative_sales": ("date_dim.d_date",),
        "brand_rank_in_store": ("store.s_store_sk", "item.i_brand"),
        "monthly_sales_change": ("date_dim.d_year", "date_dim.d_moy"),
    }.get(metric.name, ("store.s_state",))
    rows = tpcds.query(P.sql(Query(metrics=(metric.name,), dimensions=dims, limit=5))).result_rows
    assert rows
