"""Phase 0 spike, part 2: value check. Each catalog expression is evaluated in
DuckDB (reference) and in ClickHouse (after SQLGlot translation); differing
values mean a silently wrong translation."""
import re, sqlglot, duckdb, clickhouse_connect, datetime, decimal
exec(open(__file__.replace("values.py", "translate.py")).read().split("# ---- 2. Spec function catalog")[1].split("BASE = ")[0].replace("CATALOG = ", "CATALOG = ", 1)) if False else None
src = open(__file__.replace("values.py", "translate.py")).read()
CATALOG = src.split('CATALOG = """')[1].split('""".strip()')[0].strip()
ch = clickhouse_connect.get_client(host="127.0.0.1", port=8123)
dk = duckdb.connect()
CH_BASE = "(SELECT 2.5 AS x, 'abc-def' AS s, toDate('2024-03-15') AS d, toDate('2024-05-20') AS d2, toDateTime('2024-03-15 10:30:45') AS ts)"
DK_BASE = "(SELECT 2.5 AS x, 'abc-def' AS s, DATE '2024-03-15' AS d, DATE '2024-05-20' AS d2, TIMESTAMP '2024-03-15 10:30:45' AS ts)"
SKIP = ("CURRENT_",)  # time-dependent
def norm(v):
    if isinstance(v, (list, tuple)): v = v[0]
    if isinstance(v, decimal.Decimal): v = float(v)
    if isinstance(v, float): return round(v, 6)
    if isinstance(v, datetime.datetime): return v.replace(tzinfo=None)
    if isinstance(v, bool): return int(v)
    if isinstance(v, datetime.timedelta): return v.days
    return v
same = diff = ref_fail = 0; rows = []
for line in CATALOG.splitlines():
    for e in [p.strip() for p in line.split(" | ")]:
        if e.startswith(SKIP): continue
        try: ref = norm(dk.execute(f"SELECT {e} FROM {DK_BASE}").fetchone())
        except Exception as ex: ref_fail += 1; rows.append(("REF-FAIL", e, str(ex).split("\n")[0][:80], "")); continue
        try:
            t = sqlglot.parse_one(e).sql(dialect="clickhouse")
            got = norm(ch.query(f"SELECT {t} FROM {CH_BASE}").result_rows[0])
        except Exception as ex: rows.append(("CH-FAIL", e, ref, str(ex).split("\n")[0][:60])); continue
        if got == ref or (isinstance(got,(int,float)) and isinstance(ref,(int,float)) and abs(got-ref) < 1e-6): same += 1
        else: diff += 1; rows.append(("DIFF", e, ref, got))
print(f"same={same} diff={diff} ref_fail(duckdb can't eval)={ref_fail} ch_fail={sum(r[0]=='CH-FAIL' for r in rows)}\n")
for kind, e, ref, got in rows:
    if kind == "DIFF": print(f"DIFF  {e}\n      duckdb={ref!r}  clickhouse={got!r}")
print()
for kind, e, ref, got in rows:
    if kind == "REF-FAIL": print(f"REF-FAIL  {e}: {ref}")
