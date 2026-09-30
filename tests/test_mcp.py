"""MCP server: tools are thin wrappers, checked against the library directly."""

import pytest

from ossie_clickhouse import load_model
from ossie_clickhouse.mcp_server import describe, search, summary
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)

# --- pure views, no server ---------------------------------------------------


def test_summary_is_compact():
    s = summary(MODEL)
    assert [d["name"] for d in s["datasets"]] == [
        "store_sales",
        "date_dim",
        "customer",
        "item",
        "store",
    ]
    assert s["datasets"][0] == {
        "name": "store_sales",
        "description": "Fact table containing all store sales transactions",
        "synonyms": ["sales transactions", "store purchases", "retail sales", "POS data"],
        "fields": 8,
    }
    total_sales = next(m for m in s["metrics"] if m["name"] == "total_sales")
    assert total_sales["synonyms"] == ["total revenue", "gross sales", "sales amount"]
    assert "store_sales -> item" in s["relationships"]
    assert "instructions" in s


def test_search_by_synonym_and_name():
    names = {h["name"] for h in search(MODEL, "revenue")}
    assert {"total_sales", "customer_lifetime_value", "store_productivity"} <= names
    assert search(MODEL, "brand")[0]["kind"] == "field"
    assert search(MODEL, "zzz") == [] and search(MODEL, "") == []
    # words may be apart and in any order: "customer value" hits the CLV description
    assert [h["name"] for h in search(MODEL, "value customer")] == ["customer_lifetime_value"]
    assert search(MODEL, "return discount") == []


def test_describe_dataset_metric_field_and_unknown():
    d = describe(MODEL, "item")
    assert d["kind"] == "dataset" and {f["name"] for f in d["fields"]} >= {"i_brand", "i_category"}
    assert d["relationships"] == ["store_sales(ss_item_sk) -> item(i_item_sk)"]
    m = describe(MODEL, "CUMULATIVE_SALES")
    assert m["kind"] == "metric" and m["expression"].startswith("SUM(SUM(")
    assert "requires grouping by date_dim.d_date" in m["description"]
    f = describe(MODEL, "date_dim.d_date")
    assert f["kind"] == "field" and f.get("is_time")
    assert "similar" in describe(MODEL, "sales")["error"]


# --- through the protocol, against ClickHouse ---------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(clickhouse):
    from mcp import Client

    from ossie_clickhouse.mcp_server import build_server

    if not clickhouse.query("EXISTS DATABASE tpcds").result_rows[0][0]:
        pytest.skip("no tpcds database loaded (see CONTRIBUTING.md)")
    async with Client(build_server(MODEL, lambda: clickhouse), raise_exceptions=True) as c:
        yield c


@pytest.mark.anyio
async def test_tools_listed(client):
    tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {
        "list_model",
        "search_model",
        "describe_object",
        "query",
    }
    q = next(t for t in tools.tools if t.name == "query")
    assert set(q.input_schema["properties"]) == {
        "metrics",
        "dimensions",
        "filters",
        "order_by",
        "limit",
    }


@pytest.mark.anyio
async def test_tools_match_library(client):
    r = await client.call_tool("list_model", {})
    assert r.structured_content == summary(MODEL)
    r = await client.call_tool("describe_object", {"name": "total_sales"})
    assert r.structured_content == describe(MODEL, "total_sales")
    r = await client.call_tool("search_model", {"text": "profit"})
    assert r.structured_content["result"] == search(MODEL, "profit")


@pytest.mark.anyio
async def test_query_and_error(client):
    r = await client.call_tool(
        "query",
        {"metrics": ["total_sales"], "dimensions": ["item.i_category"],
         "filters": ["date_dim.d_year = 1998"], "limit": 3},
    )  # fmt: skip
    out = r.structured_content
    assert out["columns"] == ["i_category", "total_sales"] and out["row_count"] == 3
    assert "LEFT JOIN tpcds.item" in out["sql"] and "ORDER BY total_sales DESC" in out["sql"]
    values = [float(row[1]) for row in out["rows"]]
    assert values == sorted(values, reverse=True)
    r = await client.call_tool("query", {"metrics": ["total_revenue"]})
    assert not r.is_error and "did you mean: total_sales" in r.structured_content["error"]
