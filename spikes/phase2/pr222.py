"""Evaluate apache/ossie PR #222 (ossie_sql SQLGlot dialect) on the Phase 0 corpus.
For each expression: parse with read="ossie" vs default, generate ClickHouse,
execute both, and report where the Ossie dialect changes the outcome."""
import sys, yaml, sqlglot, clickhouse_connect, sqlglot.errors
import ossie_sql
from ossie_sql import validate_expression, UnsupportedConstructError

src = open("spike_translate.py").read()
CATALOG = src.split('CATALOG = """')[1].split('""".strip()')[0].strip()
MODEL = yaml.safe_load(open("ossie/examples/tpcds_semantic_model.yaml"))
ch = clickhouse_connect.get_client(host="127.0.0.1", port=8123)
BASE = ("(SELECT 2.5 AS x, 'abc-def' AS s, toDate('2024-03-15') AS d, toDate('2024-05-20') AS d2, "
        "toDateTime('2024-03-15 10:30:45') AS ts)")

def run(sql):
    try: ch.query(sql); return ""
    except Exception as e: return str(e).split("\n")[0].split("server response:")[-1].strip()[:90]

def via(expr, read):
    try:
        tree = sqlglot.parse_one(expr, read=read)
    except Exception as e:
        return None, f"PARSE: {str(e).splitlines()[0][:80]}"
    return tree, tree.sql(dialect="clickhouse")

exprs = [("catalog", e.strip()) for line in CATALOG.splitlines() for e in line.split(" | ")]
exprs += [("model", f["expression"]["dialects"][0]["expression"])
          for d in MODEL["datasets"] for f in d["fields"]]
exprs += [("model", m["expression"]["dialects"][0]["expression"]) for m in MODEL["metrics"]]

print("=== 1. Ossie dialect vs default dialect, ClickHouse output differs")
parse_fail = 0; same = 0; better = worse = neutral = 0
for kind, e in exprs:
    t_def, ch_def = via(e, None)
    t_oss, ch_oss = via(e, "ossie")
    if t_oss is None:
        parse_fail += 1; print(f"  PARSE-FAIL [{kind}] {e}: {ch_oss}"); continue
    if ch_def == ch_oss: same += 1; continue
    if kind == "catalog":
        r_def = run(f"SELECT {ch_def} FROM {BASE}") if t_def is not None else "n/a"
        r_oss = run(f"SELECT {ch_oss} FROM {BASE}")
        tag = "BETTER" if (r_def and not r_oss) else "WORSE" if (r_oss and not r_def) else "neutral"
    else:
        tag = "neutral"; r_def = r_oss = ""
    {"BETTER": lambda: globals().__setitem__("better", better + 1),
     "WORSE": lambda: globals().__setitem__("worse", worse + 1),
     "neutral": lambda: globals().__setitem__("neutral", neutral + 1)}[tag]()
    print(f"  {tag:7s} {e}\n          default: {ch_def}  {('!! ' + r_def) if r_def else ''}\n          ossie:   {ch_oss}  {('!! ' + r_oss) if r_oss else ''}")
print(f"  totals: {len(exprs)} expressions, identical={same}, better={better}, worse={worse}, neutral={neutral}, parse_fail={parse_fail}")

print("\n=== 2. validate_expression on the corpus (should all pass)")
bad = 0
for kind, e in exprs:
    try: validate_expression(sqlglot.parse_one(e, read="ossie"))
    except Exception as ex: bad += 1; print(f"  REJECTED [{kind}] {e}: {ex}")
print(f"  rejected {bad}/{len(exprs)}")

print("\n=== 3. Forbidden constructs (should all be rejected)")
for e in ["(SELECT 1)", "x IN (SELECT 1)", "SUM(x) OVER (ORDER BY d GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW)",
          "x = ?", "x = :p", "CASE WHEN EXISTS (SELECT 1) THEN 1 END"]:
    try:
        validate_expression(sqlglot.parse_one(e, read="ossie")); print(f"  ACCEPTED (bug?) {e}")
    except UnsupportedConstructError as ex: print(f"  rejected  {e}")
    except Exception as ex: print(f"  parse error {e}: {str(ex).splitlines()[0][:60]}")

print("\n=== 4. Review comments on the PR, reproduced?")
for e in ["NOT ('x IN y' IN ('a'))", "'x IN y' NOT IN ('a')", "s NOT IN ('a IN b')",
          "CHARINDEX('a', s, 3)", "APPROX_PERCENTILE(x, 0.5, 100)", "s NOT LIKE 'a%'", "x IS NOT NULL"]:
    try:
        t = sqlglot.parse_one(e, read="ossie"); print(f"  {e!r:40s} -> ossie: {t.sql(dialect='ossie')!r:44s} clickhouse: {t.sql(dialect='clickhouse')!r}")
    except Exception as ex: print(f"  {e!r:40s} -> ERROR {str(ex).splitlines()[0][:60]}")

print("\n=== 5. Identifier normalization (spec: unquoted -> UPPER)")
from ossie_sql import normalize_identifier
t = sqlglot.parse_one("store_sales.ss_ext_sales_price", read="ossie")
print("  ", [normalize_identifier(i) for i in t.find_all(sqlglot.exp.Identifier)], "| rendered:", t.sql(dialect="clickhouse"))
