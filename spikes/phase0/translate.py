"""Phase 0 spike: translate Ossie expressions (parsed as ANSI SQL) to ClickHouse
SQL with SQLGlot, then execute each translation against local ClickHouse."""
import sys, yaml, sqlglot, clickhouse_connect
from sqlglot import exp

MODEL = sys.argv[1]
ch = clickhouse_connect.get_client(host="127.0.0.1", port=8123)

def translate(expr):
    tree = sqlglot.parse_one(expr)            # default dialect = ANSI-ish
    return tree.sql(dialect="clickhouse")

def run(sql):
    try:
        ch.query(sql); return None
    except Exception as e:
        return str(e).split("\n")[0][:160]

results = []  # (kind, name, ossie_expr, ch_expr, parse_err, exec_err)

# ---- 1. Model expressions -------------------------------------------------
m = yaml.safe_load(open(MODEL))
sources = {d["name"]: d["source"].replace("tpcds.public.", "tpcds.") for d in m["datasets"]}
for d in m["datasets"]:
    for f in d["fields"]:
        e = f["expression"]["dialects"][0]["expression"]
        try:
            t = translate(e)
        except Exception as ex:
            results.append(("field", f"{d['name']}.{f['name']}", e, "", str(ex)[:120], "")); continue
        err = run(f"SELECT {t} FROM {sources[d['name']]} LIMIT 1")
        results.append(("field", f"{d['name']}.{f['name']}", e, t, "", err or ""))

# Fixed star join for metrics; group-by per metric taken from descriptions.
FROM = ("tpcds.store_sales AS store_sales "
        "JOIN tpcds.date_dim AS date_dim ON store_sales.ss_sold_date_sk = date_dim.d_date_sk "
        "JOIN tpcds.customer AS customer ON store_sales.ss_customer_sk = customer.c_customer_sk "
        "JOIN tpcds.item AS item ON store_sales.ss_item_sk = item.i_item_sk "
        "JOIN tpcds.store AS store ON store_sales.ss_store_sk = store.s_store_sk")
GROUP = {"sales_by_brand": "item.i_brand", "cumulative_sales": "date_dim.d_date",
         "brand_rank_in_store": "store.s_store_sk, item.i_brand",
         "monthly_sales_change": "date_dim.d_year, date_dim.d_moy"}
for mt in m["metrics"]:
    e = mt["expression"]["dialects"][0]["expression"]
    try:
        t = translate(e)
    except Exception as ex:
        results.append(("metric", mt["name"], e, "", str(ex)[:120], "")); continue
    g = GROUP.get(mt["name"])
    sql = f"SELECT {g + ', ' if g else ''}{t} AS v FROM {FROM}{' GROUP BY ' + g if g else ''} LIMIT 5"
    results.append(("metric", mt["name"], e, t, "", run(sql) or ""))

# ---- 2. Spec function catalog (literals only) ------------------------------
CATALOG = """
SUM(x) | COUNT(x) | COUNT(*) | COUNT(DISTINCT x) | AVG(x) | MIN(x) | MAX(x)
STDDEV(x) | STDDEV_POP(x) | STDDEV_SAMP(x) | VARIANCE(x) | VAR_POP(x) | VAR_SAMP(x)
MEDIAN(x) | PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) | PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x)
APPROX_COUNT_DISTINCT(x) | APPROX_PERCENTILE(x, 0.5) | SUM(DISTINCT x)
SUM(CASE WHEN x > 1 THEN x ELSE 0 END) | COUNT(CASE WHEN x > 1 THEN 1 END)
CURRENT_DATE | CURRENT_DATE() | CURRENT_TIMESTAMP | CURRENT_TIMESTAMP() | CURRENT_TIME | CURRENT_TIME()
YEAR(d) | QUARTER(d) | MONTH(d) | DAY(d) | DAYOFYEAR(d) | HOUR(ts) | MINUTE(ts) | SECOND(ts)
EXTRACT(YEAR FROM d) | EXTRACT(MONTH FROM d) | EXTRACT(DAY FROM d) | EXTRACT(WEEK FROM d) | EXTRACT(DAYOFWEEK FROM d) | EXTRACT(DAYOFYEAR FROM d) | EXTRACT(MILLISECOND FROM ts)
DATE_PART('year', d) | DATE_PART('month', d) | DATE_PART('day', d)
DATE_TRUNC('year', d) | DATE_TRUNC('quarter', d) | DATE_TRUNC('month', d) | DATE_TRUNC('week', d) | DATE_TRUNC('day', d) | DATE_TRUNC('hour', ts) | DATE_TRUNC('minute', ts) | DATE_TRUNC('second', ts)
DATEADD(day, 7, d) | DATEADD(month, -1, d) | DATEADD(year, 1, d)
DATEDIFF(day, d, d2) | DATEDIFF(month, d, d2) | DATEDIFF(year, d, d2)
DATE '2024-01-15' | TIMESTAMP_NTZ '2024-01-15 10:30:00' | TIME '10:30:00'
CAST('2024-01-15' AS DATE) | CAST('2024-01-15 10:30:00' AS TIMESTAMP_NTZ) | CAST('10:30:00' AS TIME)
TO_DATE('2024-01-15') | TO_TIMESTAMP('2024-01-15 10:30:00') | TO_DATE('15/01/2024', 'DD/MM/YYYY') | TO_CHAR(d, 'YYYY-MM-DD')
CONCAT(s, s) | s || s | LENGTH(s) | LOWER(s) | UPPER(s) | TRIM(s) | LTRIM(s) | RTRIM(s) | LEFT(s, 2) | RIGHT(s, 2)
SUBSTRING(s, 1, 2) | REPLACE(s, 'a', 'b') | SPLIT_PART(s, '-', 1)
POSITION('a' IN s) | CHARINDEX('a', s) | CONTAINS(s, 'a') | STARTSWITH(s, 'a') | ENDSWITH(s, 'a')
s LIKE 'a%' | s ILIKE 'A%' | REGEXP_LIKE(s, 'a.*') | REGEXP_EXTRACT(s, 'a.') | REGEXP_REPLACE(s, 'a', 'b') | REGEXP_COUNT(s, 'a')
ABS(x) | ROUND(x, 1) | FLOOR(x) | CEIL(x) | CEILING(x) | TRUNC(x, 1) | TRUNCATE(x, 1) | MOD(x, 2) | SIGN(x)
POWER(x, 2) | SQRT(x) | EXP(x) | LN(x) | LOG(10, x) | LOG10(x)
SIN(x) | COS(x) | TAN(x) | ASIN(0.5) | ACOS(0.5) | ATAN(x) | ATAN2(x, x) | RADIANS(x) | DEGREES(x) | PI()
GREATEST(x, 1, 2) | LEAST(x, 1, 2)
CASE WHEN x > 1 THEN 'a' ELSE 'b' END | CASE x WHEN 1 THEN 'a' ELSE 'b' END
IF(x > 1, 1, 0) | IFF(x > 1, 1, 0) | NULLIF(x, 1) | COALESCE(x, 1) | IFNULL(x, 1) | NVL(x, 1) | NVL2(x, 1, 0) | ZEROIFNULL(x) | NULLIFZERO(x)
x BETWEEN 1 AND 3 | x IN (1, 2, 3) | x NOT IN (1, 2) | x IS NULL | x IS NOT NULL | NOT x > 1 | x > 1 AND x < 3 OR x = 5 | x % 2 | x <> 1 | x != 1 | TRUE | FALSE
""".strip()
BASE = ("(SELECT 2.5 AS x, 'abc-def' AS s, toDate('2024-03-15') AS d, toDate('2024-05-20') AS d2, "
        "toDateTime('2024-03-15 10:30:45') AS ts)")
for line in CATALOG.splitlines():
    for e in [p.strip() for p in line.split(" | ")]:
        try:
            t = translate(e)
        except Exception as ex:
            results.append(("catalog", e, e, "", str(ex).split("\n")[0][:120], "")); continue
        results.append(("catalog", e, e, t, "", run(f"SELECT {t} FROM {BASE}") or ""))

# ---- Report -----------------------------------------------------------------
import collections
tot = collections.Counter(); ok = collections.Counter()
for kind, name, e, t, perr, xerr in results:
    tot[kind] += 1; ok[kind] += not perr and not xerr
print("SUMMARY")
for k in tot: print(f"  {k:8s} {ok[k]:3d}/{tot[k]:3d} ok")
print("\nFAILURES")
for kind, name, e, t, perr, xerr in results:
    if perr or xerr:
        print(f"[{kind}] {e}\n    -> {t or '(parse failed)'}\n    !! {perr or xerr}")
print("\nCHANGED (translated ok, but text differs)")
for kind, name, e, t, perr, xerr in results:
    if not perr and not xerr and t.replace(" ", "").upper() != e.replace(" ", "").upper():
        print(f"  {e}\n    -> {t}")
