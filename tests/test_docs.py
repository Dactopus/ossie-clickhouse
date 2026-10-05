"""docs/index.html quotes what the planner writes; these keep the quotes true."""

import html
import re
from pathlib import Path

import pytest

from ossie_clickhouse import load_model
from ossie_clickhouse.access import Hidden, restrict
from ossie_clickhouse.planner import PlanError, Planner, Query

ROOT = Path(__file__).parents[1]
PAGE = (ROOT / "docs" / "index.html").read_text()
MODEL = load_model(str(ROOT / "tests" / "fixtures" / "web_analytics.yaml"))
QUESTION = Query(
    metrics=("revenue", "purchases"),
    dimensions=("purchases.purchase_month", "sessions.source_medium", "purchases.currency"),
    filters=("sessions.country = 'United States'",),
)


def refusal(model, q: Query) -> str:
    with pytest.raises(PlanError) as e:
        Planner(model).sql(q)
    return str(e.value)


def test_page_shows_the_sql_the_planner_writes():
    shown = html.unescape(re.search(r'<pre id="sql">(.*?)</pre>', PAGE, re.S)[1])
    assert shown == Planner(MODEL).sql(QUESTION, pretty=True)


def test_page_quotes_the_fan_out_refusal():
    q = Query(metrics=("session_conversion_rate",), dimensions=("purchases.currency",))
    assert refusal(MODEL, q) in PAGE


def test_page_quotes_what_a_user_without_revenue_gets():
    trimmed = restrict(MODEL, Hidden(fields=frozenset({"purchases.revenue"})))
    metrics = {m.name for m in trimmed.metrics}
    assert {"revenue", "average_order_value"}.isdisjoint(metrics)
    assert "revenue_usd" in metrics
    assert refusal(trimmed, QUESTION) in PAGE
