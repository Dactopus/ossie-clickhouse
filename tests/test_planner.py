import pytest

from ossie_clickhouse import load_model
from ossie_clickhouse.cli import main
from ossie_clickhouse.planner import PlanError, Planner, Query, source_table
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)
P = Planner(MODEL)


def test_source_table():
    assert source_table("tpcds.public.store_sales").sql() == "tpcds.store_sales"
    assert source_table("db.t").sql() == "db.t"
    assert source_table("t").sql() == "t"
    with pytest.raises(PlanError, match="query sources"):
        source_table("SELECT * FROM t")


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
        "GROUP BY item.i_brand LIMIT 10 SETTINGS join_use_nulls = 1"
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
    assert "ORDER BY i_brand ASC NULLS FIRST, total_sales ASC NULLS FIRST" in P.sql(q)
    with pytest.raises(
        PlanError, match="not a selected metric or dimension \\(did you mean: total_sales"
    ):
        P.sql(Query(metrics=("total_sales",), order_by=("total_sale",)))
    with pytest.raises(PlanError, match="use 'name'"):
        P.sql(Query(metrics=("total_sales",), order_by=("total_sales down",)))


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
            "use dataset.field or a metric name",
        ),
        (
            Query(metrics=("total_sales",), filters=("store_sales.ss_quantity IN (SELECT 1)",)),
            "not allowed",
        ),
        (Query(dimensions=("item.i_brand", "store.s_state")), "not joined by direct relationships"),
    ],
)
def test_errors(query, message):
    with pytest.raises(PlanError, match=message.replace("(", r"\(").replace("?", r"\?")):
        P.sql(query)


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
        "SETTINGS join_use_nulls = 1"
    )
    # No dimensions: HAVING without GROUP BY is still one SELECT.
    assert P.sql(Query(metrics=("total_sales",), filters=("total_sales > 1",))).endswith(
        "HAVING SUM(store_sales.ss_ext_sales_price) > 1 SETTINGS join_use_nulls = 1"
    )


def test_join_requires_unique_key_on_target():
    import copy

    m = copy.deepcopy(MODEL.model_dump(by_alias=True))
    item = next(d for d in m["datasets"] if d["name"] == "item")
    item["primary_key"] = ["i_item_id"]
    item["unique_keys"] = None
    from ossie import OssieDocument

    p = Planner(OssieDocument.model_validate(m))
    with pytest.raises(PlanError, match="not many-to-one"):
        p.sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))


def test_cli(capsys):
    assert main(["sql", str(FIXTURE), "-m", "total_sales", "-d", "item.i_brand", "-l", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SELECT\n") and "LEFT JOIN tpcds.item" in out
    assert main(["sql", str(FIXTURE), "-m", "nope"]) == 1
    assert "unknown metric" in capsys.readouterr().err


# --- execution against TPC-DS ------------------------------------------------


@pytest.fixture(scope="module")
def tpcds(clickhouse):
    if not clickhouse.query("EXISTS DATABASE tpcds").result_rows[0][0]:
        pytest.skip("no tpcds database loaded (see spikes/phase0/README.md)")
    return clickhouse


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
        "HAVING s > 100000 ORDER BY i.i_brand NULLS FIRST SETTINGS join_use_nulls = 1"
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
