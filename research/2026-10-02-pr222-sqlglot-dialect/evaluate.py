"""Evaluate apache/ossie PR #222, the Ossie SQLGlot dialect, against ClickHouse.

Every expression in corpus.yaml and in the TPC-DS model is parsed with
SQLGlot's default dialect, with read="ossie" (the subject) and with
read="snowflake" (whose function names the spec's catalog follows), generated
as ClickHouse SQL and executed. Each result gets one label:

  ok         ClickHouse runs it and SQLGlot dropped nothing.
  parser     default dialect only: it fails, the Ossie dialect runs. The
             default dialect reads the spec's syntax differently. Never
             assigned to snowflake: its column shows which catalog
             functions SQLGlot already models, read from its "unknown".
  generator  SQLGlot knows every function in the expression (no
             exp.Anonymous), yet its ClickHouse generator emits SQL that
             ClickHouse rejects, or drops an argument (SQLGlot's
             UnsupportedError).
  unknown    ClickHouse rejects a function SQLGlot passes through by name
             (exp.Anonymous). Mapping it is the implementation's work,
             whatever the parser.

Where the SQL differs from the Ossie dialect's and both run, the full results
are compared (sorted, NaN equal to NaN, CURRENT_* skipped); a difference is
printed with the first differing row of the sorted results.

Usage: python evaluate.py MODEL.yaml [--corpus corpus.yaml] [--url URL]
"""

import argparse
import json
import logging
import os
from importlib.metadata import distribution
from pathlib import Path

import clickhouse_connect
import sqlglot
import yaml
from ossie_sql import UnsupportedConstructError, normalize_identifier, validate_expression
from sqlglot import exp
from sqlglot.errors import ErrorLevel, UnsupportedError

BASE = (
    "(SELECT 2.5 AS x, 'abc-def' AS s, toDate('2024-03-15') AS d, toDate('2024-05-20') AS d2, "
    "toDateTime('2024-03-15 10:30:45') AS ts)"
)
# Joins and groupings for the TPC-DS reference model only. LEFT JOIN with
# join_use_nulls, as the library's planner does, so unmatched rows have NULL keys.
STAR = (
    "{store_sales} AS store_sales "
    "LEFT JOIN {date_dim} AS date_dim ON store_sales.ss_sold_date_sk = date_dim.d_date_sk "
    "LEFT JOIN {customer} AS customer ON store_sales.ss_customer_sk = customer.c_customer_sk "
    "LEFT JOIN {item} AS item ON store_sales.ss_item_sk = item.i_item_sk "
    "LEFT JOIN {store} AS store ON store_sales.ss_store_sk = store.s_store_sk"
)
GROUP = {
    "sales_by_brand": "item.i_brand",
    "cumulative_sales": "date_dim.d_date",
    "brand_rank_in_store": "store.s_store_sk, item.i_brand",
    "monthly_sales_change": "date_dim.d_year, date_dim.d_moy",
}
DIALECTS = {"default": None, "ossie": "ossie", "snowflake": "snowflake"}

logging.getLogger("sqlglot").setLevel(logging.ERROR)


def model_cases(path):
    """(kind, expression, run template, compare template) for every field and metric.

    Templates take the translation for {}; the compare template has no LIMIT.
    """
    m = yaml.safe_load(Path(path).read_text())
    tables = {}
    for d in m["datasets"]:
        parts = d["source"].split(".")
        tables[d["name"]] = f"{parts[0]}.{parts[-1]}"  # db.schema.table -> db.table
        for f in d["fields"]:
            e = f["expression"]["dialects"][0]["expression"]
            sql = f"SELECT {{}} FROM {tables[d['name']]}"
            yield "field", e, sql + " LIMIT 1", sql
    star = STAR.format(**tables)
    for mt in m["metrics"]:
        e = mt["expression"]["dialects"][0]["expression"]
        g = GROUP.get(mt["name"])
        sql = f"SELECT {g + ', ' if g else ''}{{}} AS v FROM {star}"
        sql += f" GROUP BY {g}" if g else ""
        nulls = " SETTINGS join_use_nulls = 1"
        yield "metric", e, sql + " LIMIT 5" + nulls, sql + nulls


def main():
    here = Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="TPC-DS model YAML, e.g. tests/fixtures/tpcds.yaml")
    ap.add_argument("--corpus", default=here / "corpus.yaml")
    ap.add_argument("--url", help="ClickHouse HTTP URL (default $OSSIE_CLICKHOUSE_URL)")
    args = ap.parse_args()

    url = args.url or os.environ.get("OSSIE_CLICKHOUSE_URL", "http://127.0.0.1:8123")
    ch = clickhouse_connect.get_client(dsn=url, autogenerate_session_id=False)

    def run(sql):
        try:
            ch.query(sql)
            return ""
        except Exception as e:
            return str(e).split("server response:")[-1].strip().splitlines()[0][:100]

    def result(sql):
        rows = ch.query(sql).result_rows
        return sorted((tuple("nan" if v != v else v for v in row) for row in rows), key=repr)

    corpus = yaml.safe_load(Path(args.corpus).read_text())
    one_row = f"SELECT {{}} FROM {BASE}"
    cases = [("catalog", e, one_row, one_row) for group in corpus.values() for e in group]
    cases += list(model_cases(args.model))

    for name in ["sqlglot", "apache-ossie-sql"]:
        dist = distribution(name)
        src = json.loads(dist.read_text("direct_url.json") or "{}")
        commit = src.get("vcs_info", {}).get("commit_id")
        print(
            f"{name} {dist.version}"
            + (f" from {src['url']}" if src else "")
            + (f" @ {commit}" if commit else "")
        )
    print(f"clickhouse {ch.command('SELECT version()')}")
    print(
        f"corpus {sum(k == 'catalog' for k, *_ in cases)} catalog, "
        f"{sum(k != 'catalog' for k, *_ in cases)} model expressions"
    )

    def evaluate(expr, template, read):
        try:
            tree = sqlglot.parse_one(expr, read=read)
        except Exception as e:
            return "parse error", "", str(e).splitlines()[0][:100]
        try:
            sql = tree.sql(dialect="clickhouse", unsupported_level=ErrorLevel.RAISE)
            dropped = ""
        except UnsupportedError as e:
            sql = tree.sql(dialect="clickhouse")
            dropped = f"dropped by SQLGlot: {str(e).splitlines()[0][:80]}"
        err = run(template.format(sql))
        if not err and not dropped:
            return "ok", sql, ""
        if err and tree.find(exp.Anonymous):
            return "unknown", sql, err
        return "generator", sql, err or dropped

    print("\n=== 1. ClickHouse SQL per dialect, executed (only rows where something differs)")
    labels = {d: [] for d in DIALECTS}
    identical = better = worse = 0
    differs = {d: 0 for d in DIALECTS if d != "ossie"}
    for kind, e, template, compare in cases:
        r = {d: evaluate(e, template, read) for d, read in DIALECTS.items()}
        if r["default"][0] != "ok" and r["ossie"][0] == "ok":
            r["default"] = ("parser", *r["default"][1:])
        for d in DIALECTS:
            labels[d].append(r[d][0])
        identical += r["default"][1] == r["ossie"][1]
        better += r["default"][0] != "ok" and r["ossie"][0] == "ok"
        worse += r["default"][0] == "ok" and r["ossie"][0] != "ok"
        mark = {}
        for d in differs:
            if (
                r[d][1] == r["ossie"][1]
                or not r[d][0] == r["ossie"][0] == "ok"
                or "CURRENT_" in e.upper()
            ):
                continue
            mine, theirs = result(compare.format(r[d][1])), result(compare.format(r["ossie"][1]))
            if mine != theirs:
                differs[d] += 1
                row = next((a, b) for a, b in zip(mine, theirs, strict=False) if a != b)
                mark[d] = f"\n{'':26s}!! result differs from ossie, first row: {row[0]} vs {row[1]}"
        if len({sql for _, sql, _ in r.values()}) == 1 and {lb for lb, *_ in r.values()} == {"ok"}:
            continue
        print(f"  [{kind}] {e}")
        for d in DIALECTS:
            label, sql, err = r[d]
            print(
                f"      {d:9s} {label:10s} {sql}{mark.get(d, '')}"
                + (f"\n{'':26s}!! {err}" if err else "")
            )

    print(f"\n  {len(cases)} expressions; ClickHouse SQL identical in both dialects: {identical}")
    print(
        f"  Ossie dialect runs where default fails: {better}; "
        f"default runs where Ossie fails: {worse}"
    )
    print(
        "  runs in both, result differs from ossie: "
        + ", ".join(f"{d} {n}" for d, n in differs.items())
    )
    print(f"  {'label':12s}" + "".join(f"{d:>10s}" for d in DIALECTS))
    for label in ["ok", "parser", "generator", "unknown", "parse error"]:
        counts = [
            "-" if label == "parser" and d == "snowflake" else labels[d].count(label)
            for d in DIALECTS
        ]
        print(f"  {label:12s}" + "".join(f"{c:>10}" for c in counts))

    print("\n=== 2. validate_expression on the corpus (should all pass)")
    bad = 0
    for kind, e, *_ in cases:
        try:
            validate_expression(sqlglot.parse_one(e, read="ossie"))
        except Exception as ex:
            bad += 1
            print(f"  REJECTED [{kind}] {e}: {ex}")
    print(f"  rejected {bad}/{len(cases)}")

    print("\n=== 3. Constructs the spec disallows (should all be rejected)")
    for e in [
        "(SELECT 1)",
        "x IN (SELECT 1)",
        "SUM(x) OVER (ORDER BY d GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW)",
        "x = ?",
        "x = :p",
        "CASE WHEN EXISTS (SELECT 1) THEN 1 END",
    ]:
        try:
            validate_expression(sqlglot.parse_one(e, read="ossie"))
            print(f"  ACCEPTED  {e}")
        except UnsupportedConstructError:
            print(f"  rejected  {e}")
        except Exception as ex:
            print(f"  parse error {e}: {str(ex).splitlines()[0][:60]}")

    print("\n=== 4. Code issues from the PR review (2026-09-27)")
    for e in [
        "'x IN y' NOT IN ('a')",
        "s NOT IN ('a IN b')",
        "CHARINDEX('a', s, 3)",
        "APPROX_PERCENTILE(x, 0.5, 100)",
    ]:
        t = sqlglot.parse_one(e, read="ossie")
        print(
            f"  {e!r:34s} ossie: {t.sql(dialect='ossie')!r:38s} "
            f"clickhouse: {t.sql(dialect='clickhouse')!r}"
        )

    print("\n=== 5. Identifier normalization (spec: unquoted -> upper case)")
    t = sqlglot.parse_one("store_sales.ss_ext_sales_price", read="ossie")
    print(
        f"  {[normalize_identifier(i) for i in t.find_all(exp.Identifier)]}"
        f" rendered: {t.sql(dialect='clickhouse')}"
    )


if __name__ == "__main__":
    main()
