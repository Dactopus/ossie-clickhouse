import pytest
from ossie import OssieDialect, OssieDialectExpression, OssieExpression

from ossie_clickhouse.translate import convert_format, pick_expression, to_clickhouse, translate


def dialects(**kw) -> OssieExpression:
    return OssieExpression(
        dialects=[
            OssieDialectExpression(dialect=OssieDialect[k], expression=v) for k, v in kw.items()
        ]
    )


def test_pick_prefers_ossie_sql_then_ansi():
    assert pick_expression(dialects(ANSI_SQL="a", OSSIE_SQL_2026="o")) == "o"
    assert pick_expression(dialects(DAX="x", ANSI_SQL="a")) == "a"
    with pytest.raises(ValueError, match="has DAX"):
        pick_expression(dialects(DAX="x"))


def test_translate_uses_picked_dialect():
    assert translate(dialects(ANSI_SQL="SUM(x)", DAX="SUM([x])")) == "SUM(x)"


@pytest.mark.parametrize(
    ("ossie", "expected"),
    [
        # parser: spec argument order
        ("DATEDIFF(day, d, d2)", "DATE_DIFF(DAY, d, d2)"),
        ("DATEADD(month, -1, d)", "DATE_ADD(MONTH, -1, d)"),
        ("DATE_PART('year', d)", "EXTRACT(YEAR FROM d)"),
        # aggregates
        ("VAR_POP(x)", "varPop(x)"),
        ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)", "quantile(0.5)(x)"),
        ("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY x DESC)", "quantile(1 - 0.9)(x)"),
        (
            "PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY x DESC)",
            "if(COUNT(x) = 0, NULL, arrayElement(arrayReverseSort(groupArray(x)), "
            "toUInt64(GREATEST(CEIL(0.9 * COUNT(x)), 1))))",
        ),
        ("APPROX_PERCENTILE(x, 0.5)", "quantileTDigest(0.5)(x)"),
        ("APPROX_COUNT_DISTINCT(x)", "uniq(x)"),
        # date/time
        ("CURRENT_TIME", "toTime(now())"),
        ("DAYOFYEAR(d)", "toDayOfYear(d)"),
        ("EXTRACT(DAYOFWEEK FROM d)", "toDayOfWeek(d)"),
        ("EXTRACT(DAYOFYEAR FROM d)", "toDayOfYear(d)"),
        ("TO_DATE('2024-01-15')", "toDate('2024-01-15')"),
        ("TO_DATE('15/01/2024', 'DD/MM/YYYY')", "toDate(parseDateTime('15/01/2024', '%d/%m/%Y'))"),
        ("TO_TIMESTAMP('2024-01-15 10:30:00')", "parseDateTimeBestEffort('2024-01-15 10:30:00')"),
        ("TO_CHAR(d, 'YYYY-MM-DD HH24:MI:SS')", "formatDateTime(d, '%Y-%m-%d %H:%i:%S')"),
        # strings
        ("SPLIT_PART(s, '-', 2)", "arrayElement(splitByString('-', s), 2)"),
        ("CONTAINS(s, 'a')", "position(s, 'a') > 0"),
        ("REGEXP_COUNT(s, 'a')", "countMatches(s, 'a')"),
        # conditionals
        ("IFF(x > 1, 1, 0)", "if(x > 1, 1, 0)"),
        ("ZEROIFNULL(x)", "ifNull(x, 0)"),
        ("NULLIFZERO(x)", "nullIf(x, 0)"),
        # untouched: SQLGlot's ClickHouse dialect already handles these
        ("STDDEV(x)", "stddevSamp(x)"),
        ("LENGTH(s)", "CHAR_LENGTH(s)"),
        ("store_sales.ss_ext_sales_price", "store_sales.ss_ext_sales_price"),
    ],
)
def test_to_clickhouse(ossie, expected):
    assert to_clickhouse(ossie) == expected


def test_convert_format_tokens():
    assert convert_format("DD MON YYYY, DAY HH12:MI PM 100%") == "%d %b %Y, %W %I:%i %p 100%%"
    assert convert_format("MONTH MM MI") == "%M %m %i"


def test_format_must_be_literal():
    with pytest.raises(ValueError, match="string literal"):
        to_clickhouse("TO_CHAR(d, fmt_col)")


@pytest.mark.parametrize(
    ("ossie", "message"),
    [
        # longer forms from other engines: a rewrite would drop the extra arguments
        ("REGEXP_COUNT(s, 'a', 3, 'i')", r"expected REGEXP_COUNT\(str, pattern\), got 4"),
        ("CONTAINS(s, 'a', 'x')", r"expected CONTAINS\(str, substr\), got 3"),
        ("SPLIT_PART(s, ',', 1, 2)", r"expected SPLIT_PART\(str, delimiter, part\), got 4"),
        ("TO_CHAR(d, 'YYYY', 'x')", r"expected TO_CHAR\(date_expr, format\), got 3"),
        ("TO_DATE(s, 'YYYY', 'x')", r"expected TO_DATE\(string\[, format\]\), got 3"),
        ("TO_TIMESTAMP(s, 'YYYY', 'x')", r"expected TO_TIMESTAMP\(string\[, format\]\), got 3"),
        ("APPROX_PERCENTILE(x, 0.5, 100)", r"expected APPROX_PERCENTILE\(expr, p\), got 3"),
        ("ZEROIFNULL(x, 5)", r"expected ZEROIFNULL\(expr\), got 2"),
        ("NULLIFZERO(x, 5)", r"expected NULLIFZERO\(expr\), got 2"),
        ("IFF(a, 1, 2, 3)", r"expected IFF\(condition, true_result, false_result\), got 4"),
        ("CURRENT_TIME(3)", r"expected CURRENT_TIME\(\), got 1"),
        ("DATEDIFF(day, a, b, c)", r"expected DATEDIFF\(part, start_date, end_date\), got 4"),
        ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x, y)", r"got 2 ORDER BY keys"),
        ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x, y)", r"got 2 ORDER BY keys"),
        ("PERCENTILE_CONT(0.5, 1) WITHIN GROUP (ORDER BY x)", r"got 2 argument"),
        ("PERCENTILE_CONT(0.5)", r"expected PERCENTILE_CONT\(p\) WITHIN GROUP"),
        # too few: a clear error, not an IndexError
        ("APPROX_PERCENTILE(x)", r"got 1"),
        ("TO_DATE()", r"got 0"),
        ("DATEADD(day, 7)", r"got 2"),
        ("DATE_PART()", r"expected DATE_PART\(part, date_expr\), got 0"),
        ("REGEXP_COUNT(s)", r"expected REGEXP_COUNT\(str, pattern\), got 1"),
        ("CONTAINS(s)", r"expected CONTAINS\(str, substr\), got 1"),
        # a column where the spec wants a date part
        ("DATE_PART(p, d)", r"P is not a date part"),
        ("DATE_PART(t.year, d)", r"t.year is not a date part"),
        ("EXTRACT(EPOCH FROM d)", r"EPOCH is not a date part"),
        # does not tokenize
        ("'abc", r"cannot parse"),
    ],
)
def test_rejects_arguments_outside_spec_signature(ossie, message):
    with pytest.raises(ValueError, match=message):
        to_clickhouse(ossie)
