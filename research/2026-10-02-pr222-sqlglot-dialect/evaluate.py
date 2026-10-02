"""Evaluate apache/ossie PR #222, the Ossie SQLGlot dialect, against ClickHouse.

Every expression in corpus.yaml and in the TPC-DS model is parsed with
SQLGlot's default dialect, with read="ossie" (the subject) and with
read="snowflake" as a third reading, generated as ClickHouse SQL and
executed. Each result gets one label:

  ok         ClickHouse runs it and SQLGlot dropped nothing.
  parser     default dialect only: it fails, the Ossie dialect runs. The
             default dialect reads the spec's syntax differently. Never
             assigned to snowflake: its column shows which catalog
             functions SQLGlot already models, read from its "unknown".
  generator  ClickHouse rejects the SQL from SQLGlot's ClickHouse generator,
             or SQLGlot drops an argument (its UnsupportedError), and the
             rejection is not "unknown".
  unknown    ClickHouse reports UNKNOWN_FUNCTION for a function SQLGlot
             passed through by name (exp.Anonymous). Mapping it is the
             implementation's work, whatever the parser.
  error      ClickHouse failed for another reason (memory, timeout, missing
             table, network). Not a finding: the run exits with status 1.

ClickHouse "rejects" means an error whose name is in REJECTED.

Where the SQL differs from the Ossie dialect's and both run, the full results
are compared (sorted, NaN equal to NaN, CURRENT_* skipped); a difference is
printed with the first differing row of the sorted results.

Usage: python evaluate.py MODEL.yaml [--corpus corpus.yaml] [--url URL]

MODEL is the TPC-DS reference model; the joins and groupings below are fixed
for it.
"""

import argparse
import json
import logging
import os
import re
import sys
from importlib.metadata import distribution
from pathlib import Path

import clickhouse_connect
import sqlglot
import yaml
from clickhouse_connect.driver.exceptions import DatabaseError
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
# ClickHouse error names for "analysed the SQL and refused it".
REJECTED = {
    "SYNTAX_ERROR",
    "UNKNOWN_FUNCTION",
    "UNKNOWN_IDENTIFIER",
    "NUMBER_OF_ARGUMENTS_DOESNT_MATCH",
    "ILLEGAL_TYPE_OF_ARGUMENT",
    "TYPE_MISMATCH",
    "BAD_ARGUMENTS",
}

logging.getLogger("sqlglot").setLevel(logging.ERROR)


def expression(obj):
    """The expression in the dialect translate.py prefers; anything else stops the run."""
    by_dialect = {d["dialect"]: d["expression"] for d in obj["expression"]["dialects"]}
    for dialect in ("OSSIE_SQL_2026", "ANSI_SQL"):
        if dialect in by_dialect:
            return by_dialect[dialect]
    sys.exit(f"{obj['name']}: no OSSIE_SQL_2026 or ANSI_SQL expression")


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
            e = expression(f)
            sql = f"SELECT {{}} FROM {tables[d['name']]}"
            yield "field", e, sql + " LIMIT 1", sql
    star = STAR.format(**tables)
    for mt in m["metrics"]:
        e = expression(mt)
        g = GROUP.get(mt["name"])
        sql = f"SELECT {g + ', ' if g else ''}{{}} AS v FROM {star}"
        sql += f" GROUP BY {g}" if g else ""
        nulls = " SETTINGS join_use_nulls = 1"
        yield "metric", e, sql + " LIMIT 5" + nulls, sql + nulls


def main():
    here = Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="the TPC-DS reference model, tests/fixtures/tpcds.yaml")
    ap.add_argument("--corpus", default=here / "corpus.yaml")
    ap.add_argument("--url", help="ClickHouse HTTP URL (default $OSSIE_CLICKHOUSE_URL)")
    args = ap.parse_args()

    url = args.url or os.environ.get("OSSIE_CLICKHOUSE_URL", "http://127.0.0.1:8123")
    ch = clickhouse_connect.get_client(dsn=url, autogenerate_session_id=False)

    errors = []

    def query(sql):
        """(rows, message, error name); the name is "error" unless ClickHouse rejected the SQL."""
        try:
            return ch.query(sql).result_rows, "", None
        except Exception as e:
            lines = str(e).split("server response:")[-1].strip().splitlines()
            message = lines[0] if lines else type(e).__name__
            name = e.name if isinstance(e, DatabaseError) else None
            if name not in REJECTED:
                errors.append(f"{message[:100]}\n    in: {sql[:200]}")
                return [], message, "error"
            return [], message, name

    def result(sql):
        rows, message, name = query(sql)
        rows = sorted((tuple("nan" if v != v else v for v in row) for row in rows), key=repr)
        return rows, message if name else ""

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
        _, err, name = query(template.format(sql))
        if name == "error":
            return "error", sql, err[:100]
        if not err and not dropped:
            return "ok", sql, ""
        missing = re.search(r"Function with name `([^`]+)`", err)
        anonymous = {a.name.upper() for a in tree.find_all(exp.Anonymous)}
        if name == "UNKNOWN_FUNCTION" and missing and missing.group(1).upper() in anonymous:
            return "unknown", sql, err[:100]
        return "generator", sql, err[:100] or dropped

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
            (mine, err), (theirs, err2) = (
                result(compare.format(r[d][1])),
                result(compare.format(r["ossie"][1])),
            )
            if err or err2:
                mark[d] = f"\n{'':26s}!! comparison failed: {(err or err2)[:100]}"
            elif mine != theirs:
                differs[d] += 1
                row = next(
                    ((a, b) for a, b in zip(mine, theirs, strict=False) if a != b),
                    (f"{len(mine)} rows", f"{len(theirs)} rows"),
                )
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

    if errors:
        print(f"\n=== {len(errors)} ClickHouse errors that are not SQL rejections; the run is void")
        for error in errors:
            print(f"  {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
