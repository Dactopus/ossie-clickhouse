"""Translate Ossie expressions to ClickHouse SQL.

Parsing uses SQLGlot's default (ANSI-like) dialect plus the few spec shapes
it gets wrong. Generation uses SQLGlot's ClickHouse dialect after rewriting
the spec functions ClickHouse lacks. Every rewrite works on the AST, never on
SQL text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlglot
from ossie import OssieDialect, OssieExpression
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.helper import seq_get
from sqlglot.parser import Parser

# Preference order when a field or metric carries several dialects.
DIALECT_PREFERENCE = (OssieDialect.OSSIE_SQL_2026, OssieDialect.ANSI_SQL)


def pick_expression(expression: OssieExpression) -> str:
    """The expression text in the most preferred dialect we can translate."""
    by_dialect = {d.dialect: d.expression for d in expression.dialects}
    for dialect in DIALECT_PREFERENCE:
        if dialect in by_dialect:
            return by_dialect[dialect]
    have = ", ".join(sorted(d.dialect.value for d in expression.dialects))
    raise ValueError(f"no expression in a supported dialect (has {have})")


# --- parsing ----------------------------------------------------------------


def _date_delta(cls):
    """Spec puts the date part first: DATEADD(day, 7, d), DATEDIFF(day, d1, d2)."""

    def build(args: Sequence[exp.Expression]) -> exp.Expression:
        return cls(this=seq_get(args, 2), expression=seq_get(args, 1), unit=seq_get(args, 0))

    return build


def _date_part(args: Sequence[exp.Expression]) -> exp.Expression:
    """DATE_PART('year', d) is EXTRACT(YEAR FROM d)."""
    part = seq_get(args, 0)
    return exp.Extract(this=exp.var(part.name.upper()), expression=seq_get(args, 1))


class _OssieParser(Parser):
    FUNCTIONS = {
        **Parser.FUNCTIONS,
        "DATEADD": _date_delta(exp.DateAdd),
        "DATEDIFF": _date_delta(exp.DateDiff),
        "DATE_PART": _date_part,
    }


class OssieSQL(Dialect):
    """ponytail: mirrors apache/ossie PR #222; replace with read="ossie" once it merges."""

    Parser = _OssieParser


def parse(expression: str) -> exp.Expression:
    return sqlglot.parse_one(expression, read=OssieSQL)


# --- ClickHouse rewrites ----------------------------------------------------


def _f(name: str, *args: exp.Expression | None) -> exp.Anonymous:
    return exp.Anonymous(this=name, expressions=[a for a in args if a is not None])


def _param_agg(name: str, param: exp.Expression, arg: exp.Expression) -> exp.Expression:
    """ClickHouse parametric aggregate: name(param)(arg)."""
    return exp.ParameterizedAgg(this=exp.to_identifier(name), expressions=[param], params=[arg])


# Spec TO_CHAR / TO_DATE format tokens -> ClickHouse formatDateTime / parseDateTime.
_FORMAT_TOKENS = {
    "YYYY": "%Y", "YY": "%y", "MONTH": "%M", "MON": "%b", "MM": "%m", "MI": "%i",
    "DD": "%d", "DAY": "%W", "DY": "%a", "HH24": "%H", "HH12": "%I", "HH": "%I",
    "SS": "%S", "AM": "%p", "PM": "%p",
}  # fmt: skip
_FORMAT_RE = re.compile("|".join(sorted(_FORMAT_TOKENS, key=len, reverse=True)) + "|%")


def convert_format(fmt: str) -> str:
    return _FORMAT_RE.sub(lambda m: "%%" if m[0] == "%" else _FORMAT_TOKENS[m[0]], fmt)


def _fmt_literal(node: exp.Expression | None) -> exp.Literal:
    if not isinstance(node, exp.Literal) or not node.is_string:
        raise ValueError(
            f"date format must be a string literal, got {node.sql() if node else None}"
        )
    return exp.Literal.string(convert_format(node.this))


_EXTRACT_PARTS = {
    "DAYOFWEEK": "toDayOfWeek",
    "DAYOFYEAR": "toDayOfYear",
    "MILLISECOND": "toMillisecond",
}

# Functions SQLGlot keeps as exp.Anonymous (no dedicated node): name -> rewrite(args).
_ANONYMOUS = {
    "APPROX_PERCENTILE": lambda a: _param_agg("quantileTDigest", a[1], a[0]),
    "TO_DATE": lambda a: (
        _f("toDate", a[0])
        if len(a) == 1
        else _f("toDate", _f("parseDateTime", a[0], _fmt_literal(a[1])))
    ),
    "TO_TIMESTAMP": lambda a: (
        _f("parseDateTimeBestEffort", a[0])
        if len(a) == 1
        else _f("parseDateTime", a[0], _fmt_literal(a[1]))
    ),
    "IFF": lambda a: _f("if", *a),
    "ZEROIFNULL": lambda a: _f("ifNull", a[0], exp.Literal.number(0)),
    "NULLIFZERO": lambda a: _f("nullIf", a[0], exp.Literal.number(0)),
}


def _rewrite(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Anonymous):
        fn = _ANONYMOUS.get(node.name.upper())
        return fn(node.expressions) if fn else node
    if isinstance(node, exp.WithinGroup) and isinstance(
        node.this, exp.PercentileCont | exp.PercentileDisc
    ):
        order = node.expression.expressions[0]
        p = node.this.this
        if order.args.get("desc"):
            p = exp.Sub(this=exp.Literal.number(1), expression=p)
        name = "quantile" if isinstance(node.this, exp.PercentileCont) else "quantileExact"
        return _param_agg(name, p, order.this)
    if isinstance(node, exp.VariancePop):
        return _f("varPop", node.this)
    if isinstance(node, exp.CurrentTime):
        return _f("toTime", _f("now"))
    if isinstance(node, exp.DayOfYear):
        return _f("toDayOfYear", node.this)
    if isinstance(node, exp.Extract) and node.name.upper() in _EXTRACT_PARTS:
        return _f(_EXTRACT_PARTS[node.name.upper()], node.expression)
    if isinstance(node, exp.ToChar):
        return _f("formatDateTime", node.this, _fmt_literal(node.args.get("format")))
    if isinstance(node, exp.SplitPart):
        parts = _f("splitByString", node.args["delimiter"], node.this)
        return _f("arrayElement", parts, node.args["part_index"])
    if isinstance(node, exp.RegexpCount):
        return _f("countMatches", node.this, node.expression)
    if isinstance(node, exp.Contains):
        return exp.GT(
            this=_f("position", node.this, node.expression), expression=exp.Literal.number(0)
        )
    return node


def to_clickhouse(expression: str | exp.Expression) -> str:
    """ClickHouse SQL for an Ossie expression."""
    tree = parse(expression) if isinstance(expression, str) else expression
    return tree.transform(_rewrite).sql(dialect="clickhouse")


def translate(expression: OssieExpression) -> str:
    """ClickHouse SQL for a model field or metric expression."""
    return to_clickhouse(pick_expression(expression))
