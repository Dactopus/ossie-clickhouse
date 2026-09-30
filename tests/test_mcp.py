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


def test_ai_context_forms():
    from ossie import OssieDocument

    data = MODEL.model_dump(by_alias=True)
    data["ai_context"] = "plain text instructions"
    metric = next(m for m in data["metrics"] if m["name"] == "total_sales")
    metric["ai_context"] = {"instructions": "sum, never average", "examples": ["sales by brand"]}
    m = OssieDocument.model_validate(data)
    assert summary(m)["instructions"] == "plain text instructions"
    d = describe(m, "total_sales")
    assert d["instructions"] == "sum, never average" and d["examples"] == ["sales by brand"]


def test_describe_shows_the_expression_the_planner_runs():
    data = MODEL.model_dump(by_alias=True)
    metric = next(m for m in data["metrics"] if m["name"] == "total_sales")
    metric["expression"]["dialects"].insert(
        0, {"dialect": "SNOWFLAKE", "expression": "SUM(store_sales.ss_ext_sales_price):snow"}
    )
    from ossie import OssieDocument

    m = describe(OssieDocument.model_validate(data), "total_sales")
    assert m["expression"] == "SUM(store_sales.ss_ext_sales_price)"


# --- through the protocol, against ClickHouse ---------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(tpcds):
    from mcp import Client

    from ossie_clickhouse.mcp_server import build_server

    async with Client(build_server(MODEL, lambda: tpcds), raise_exceptions=True) as c:
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
    # A query that plans but fails in ClickHouse is a result too, never an exception.
    r = await client.call_tool(
        "query", {"metrics": ["total_sales"], "filters": ["date_dim.d_year = 'abc'"]}
    )
    assert not r.is_error and r.structured_content["error"].startswith("ClickHouse: ")
    assert "\n" not in r.structured_content["error"]
