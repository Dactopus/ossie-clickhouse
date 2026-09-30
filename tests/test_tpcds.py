"""Every expression of the TPC-DS reference model executes in ClickHouse."""

import pytest

from ossie_clickhouse import load_model
from ossie_clickhouse.translate import translate
from tests.test_model import FIXTURE

# Fixed star join for metrics until the planner exists (Phase 3).
FROM = (
    "tpcds.store_sales AS store_sales "
    "JOIN tpcds.date_dim AS date_dim ON store_sales.ss_sold_date_sk = date_dim.d_date_sk "
    "JOIN tpcds.customer AS customer ON store_sales.ss_customer_sk = customer.c_customer_sk "
    "JOIN tpcds.item AS item ON store_sales.ss_item_sk = item.i_item_sk "
    "JOIN tpcds.store AS store ON store_sales.ss_store_sk = store.s_store_sk"
)
GROUP_BY = {
    "sales_by_brand": "item.i_brand",
    "cumulative_sales": "date_dim.d_date",
    "brand_rank_in_store": "store.s_store_sk, item.i_brand",
    "monthly_sales_change": "date_dim.d_year, date_dim.d_moy",
}
MODEL = load_model(FIXTURE)


@pytest.fixture(scope="module")
def tpcds(clickhouse):
    if not clickhouse.query("EXISTS DATABASE tpcds").result_rows[0][0]:
        pytest.skip("no tpcds database loaded (see spikes/phase0/README.md)")
    return clickhouse


@pytest.mark.parametrize(
    ("dataset", "field"),
    [(d, f) for d in MODEL.datasets for f in d.fields],
    ids=[f"{d.name}.{f.name}" for d in MODEL.datasets for f in d.fields],
)
def test_field(dataset, field, tpcds):
    table = dataset.source.replace("tpcds.public.", "tpcds.")  # source mapping rule: Phase 4
    tpcds.query(f"SELECT {translate(field.expression)} FROM {table} LIMIT 1")


@pytest.mark.parametrize("metric", MODEL.metrics, ids=[m.name for m in MODEL.metrics])
def test_metric(metric, tpcds):
    group = GROUP_BY.get(metric.name)
    sql = f"SELECT {group + ', ' if group else ''}{translate(metric.expression)} AS v FROM {FROM}"
    if group:
        sql += f" GROUP BY {group}"
    rows = tpcds.query(sql + " LIMIT 3").result_rows
    assert any(r[-1] is not None for r in rows)  # LAG leaves the first row NULL
