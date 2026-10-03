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
import sqlglot.errors
from ossie import OssieDialect, OssieDocument, OssieExpression
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
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


# Spec signatures of the functions rewritten below, checked on the arguments as
# written. The default dialect also parses other engines' longer forms, such as
# Snowflake's REGEXP_COUNT(str, pattern, position, flags); a rewrite would drop
# the extra arguments and return a different value, so parse() rejects them.
_SIGNATURES = {
    "APPROX_PERCENTILE": ("APPROX_PERCENTILE(expr, p)", 2, 2),
    "TO_DATE": ("TO_DATE(string[, format])", 1, 2),
    "TO_TIMESTAMP": ("TO_TIMESTAMP(string[, format])", 1, 2),
    "IFF": ("IFF(condition, true_result, false_result)", 3, 3),
    "ZEROIFNULL": ("ZEROIFNULL(expr)", 1, 1),
    "NULLIFZERO": ("NULLIFZERO(expr)", 1, 1),
    "PERCENTILE_CONT": ("PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)", 1, 1),
    "PERCENTILE_DISC": ("PERCENTILE_DISC(p) WITHIN GROUP (ORDER BY expr)", 1, 1),
    "CURRENT_TIME": ("CURRENT_TIME()", 0, 0),
    "TO_CHAR": ("TO_CHAR(date_expr, format)", 2, 2),
    "SPLIT_PART": ("SPLIT_PART(str, delimiter, part)", 3, 3),
    "REGEXP_COUNT": ("REGEXP_COUNT(str, pattern)", 2, 2),
    "CONTAINS": ("CONTAINS(str, substr)", 2, 2),
    "DATEADD": ("DATEADD(part, amount, date_expr)", 3, 3),
    "DATEDIFF": ("DATEDIFF(part, start_date, end_date)", 3, 3),
    "DATE_PART": ("DATE_PART(part, date_expr)", 2, 2),
    "LAG": ("LAG(expr[, offset[, default]])", 1, 3),
    "LEAD": ("LEAD(expr[, offset[, default]])", 1, 3),
    "NTH_VALUE": ("NTH_VALUE(expr, n)", 2, 2),
    "FIRST_VALUE": ("FIRST_VALUE(expr)", 1, 1),
    "LAST_VALUE": ("LAST_VALUE(expr)", 1, 1),
}

# Spec: supported date parts for EXTRACT and DATE_PART.
_DATE_PARTS = {
    "YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "DAYOFWEEK", "DAYOFYEAR",
    "HOUR", "MINUTE", "SECOND", "MILLISECOND",
}  # fmt: skip


def _date_delta(cls):
    """Spec puts the date part first: DATEADD(day, 7, d), DATEDIFF(day, d1, d2)."""

    def build(args: Sequence[exp.Expression]) -> exp.Expression:
        return cls(this=args[2], expression=args[1], unit=args[0])

    return build


def _date_part(args: Sequence[exp.Expression]) -> exp.Expression:
    """DATE_PART('year', d) is EXTRACT(YEAR FROM d); parse() checks the part."""
    part = args[0]
    if not (part.is_string or isinstance(part, exp.Column) and not part.table):
        raise sqlglot.errors.ParseError(f"DATE_PART: {part.sql()} is not a date part")
    return exp.Extract(this=exp.var(part.name.upper()), expression=args[1])


def _checked(name: str, build=None):
    """The builder for ``name``, rejecting argument counts outside its spec signature."""
    signature, low, high = _SIGNATURES[name]
    build = (
        build
        or Parser.FUNCTIONS.get(name)
        or (lambda args: exp.Anonymous(this=name, expressions=args))
    )

    def checked(args: Sequence[exp.Expression], **kwargs) -> exp.Expression:
        if not low <= len(args) <= high:
            raise sqlglot.errors.ParseError(f"expected {signature}, got {len(args)} argument(s)")
        return build(args, **kwargs)  # SQLGlot retries with dialect= on TypeError

    return checked


_BUILDERS = {
    "DATEADD": _date_delta(exp.DateAdd),
    "DATEDIFF": _date_delta(exp.DateDiff),
    "DATE_PART": _date_part,
}


class _OssieParser(Parser):
    FUNCTIONS = {
        **Parser.FUNCTIONS,
        **{name: _checked(name, _BUILDERS.get(name)) for name in _SIGNATURES},
    }

    def _parse_ordered(self, parse_method=None):
        ordered = super()._parse_ordered(parse_method)
        # The spec's ORDER BY has no NULLS FIRST | LAST (docs/design.md). The AST cannot
        # tell an explicit NULLS LAST from the default, so look at the tokens just read.
        last = [t.text.upper() for t in self._tokens[max(self._index - 2, 0) : self._index]]
        if ordered and last in (["NULLS", "FIRST"], ["NULLS", "LAST"]):
            self.raise_error("NULLS FIRST | LAST is not part of the Ossie ORDER BY syntax")
        return ordered


class OssieSQL(Dialect):
    """SQLGlot's default dialect plus the spec shapes it parses differently.

    Mirrors apache/ossie PR #222; switch to ``read="ossie"`` once it merges."""

    Parser = _OssieParser
    # Spec is silent; NULLs sort last both ways, as in ClickHouse and DuckDB (docs/design.md).
    NULL_ORDERING = "nulls_are_last"


_DISALLOWED = (
    exp.Select, exp.Subquery, exp.With, exp.Union, exp.Intersect, exp.Except, exp.Join,
    exp.Create, exp.Drop, exp.Alter, exp.Insert, exp.Update, exp.Delete,
    exp.Placeholder, exp.Parameter,
)  # fmt: skip


def _bound(value: str | exp.Expression | None, side: str | None) -> str:
    """A frame bound in upper case, a number as N: UNBOUNDED PRECEDING, N PRECEDING..."""
    if isinstance(value, exp.Literal) and value.is_number:
        value = "N"
    return f"{value or 'CURRENT ROW'} {side or ''}".strip().upper()


# Spec frames: ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW, ROWS BETWEEN n PRECEDING
# AND n FOLLOWING, RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW; its examples also
# use n PRECEDING AND CURRENT ROW. ROWS frames that hold the current row are accepted.
# Others go wrong in ClickHouse: an empty frame sums to 0, not NULL, and RANGE offsets
# put NULL keys within any offset.
_ROWS_STARTS = {"UNBOUNDED PRECEDING", "N PRECEDING", "CURRENT ROW"}
_ROWS_ENDS = {"CURRENT ROW", "N FOLLOWING", "UNBOUNDED FOLLOWING"}


def _frame_problem(spec: exp.WindowSpec) -> str | None:
    kind = (spec.args.get("kind") or "").upper()
    start = _bound(spec.args.get("start"), spec.args.get("start_side"))
    end = _bound(spec.args.get("end"), spec.args.get("end_side"))
    if kind == "ROWS" and start in _ROWS_STARTS and end in _ROWS_ENDS:
        return None
    if (kind, start, end) == ("RANGE", "UNBOUNDED PRECEDING", "CURRENT ROW"):
        return None
    return (
        f"window frame {spec.sql()} is not supported; use ROWS BETWEEN UNBOUNDED or "
        "n PRECEDING (or CURRENT ROW) AND CURRENT ROW (or n FOLLOWING), or "
        "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
    )


def _shape_problem(node: exp.Expression) -> str | None:
    """Spec shapes an argument count cannot express."""
    if isinstance(node, exp.PercentileCont | exp.PercentileDisc):
        signature = _SIGNATURES[node.sql_name()][0]
        if not isinstance(node.parent, exp.WithinGroup):
            return f"expected {signature}"
        keys = len(node.parent.expression.expressions)
        if keys != 1:
            return f"expected {signature}, got {keys} ORDER BY keys"
    if isinstance(node, exp.Extract) and node.name.upper() not in _DATE_PARTS:
        return f"{node.name} is not a date part; expected one of " + ", ".join(sorted(_DATE_PARTS))
    if isinstance(node, exp.WindowSpec):
        return _frame_problem(node)
    # ClickHouse accepts IGNORE NULLS here and silently ignores it.
    if isinstance(node, exp.IgnoreNulls) and isinstance(
        node.this, exp.Lag | exp.Lead | exp.NthValue
    ):
        return f"IGNORE NULLS is not supported for {node.this.sql_name()}"
    return None


def parse(expression: str) -> exp.Expression:
    """Parse an Ossie expression, rejecting constructs the spec disallows."""
    try:
        tree = sqlglot.parse_one(expression, read=OssieSQL)
    except sqlglot.errors.SqlglotError as e:
        raise ValueError(f"cannot parse {expression!r}: {str(e).splitlines()[0]}") from e
    for node in tree.walk():
        if isinstance(node, _DISALLOWED):
            raise ValueError(
                f"{type(node).__name__} is not allowed in an Ossie expression: {expression!r}"
            )
        if problem := _shape_problem(node):
            raise ValueError(f"{problem}: {expression!r}")
    return tree


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


def rewrite(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Anonymous):
        fn = _ANONYMOUS.get(node.name.upper())
        return fn(node.expressions) if fn else node
    if isinstance(node, exp.WithinGroup) and isinstance(
        node.this, exp.PercentileCont | exp.PercentileDisc
    ):
        order = node.expression.expressions[0]
        p, arg, desc = node.this.this, order.this, bool(order.args.get("desc"))
        if isinstance(node.this, exp.PercentileCont):
            if desc:
                p = exp.Sub(this=exp.Literal.number(1), expression=p)
            return _param_agg("quantile", p, arg)
        # Spec (Postgres semantics): the first value whose cumulative share reaches p,
        # so element ceil(p * n) of the sorted values (1-based, at least 1), NULL for
        # an empty set. quantileExact takes element floor(p * n) + 1: one too high
        # whenever p * n is whole.
        n = exp.Count(this=arg)
        index = _f(
            "toUInt64",
            exp.Greatest(
                this=exp.Ceil(this=exp.Mul(this=p, expression=n)),
                expressions=[exp.Literal.number(1)],
            ),
        )
        values = _f(
            "arrayReverseSort" if desc else "arraySort",
            exp.AnonymousAggFunc(this="groupArray", expressions=[arg]),
        )
        return _f(
            "if",
            exp.EQ(this=n.copy(), expression=exp.Literal.number(0)),
            exp.Null(),
            _f("arrayElement", values, index),
        )
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
    # Spec windows follow ANSI SQL: NULL when the row does not exist, and NULLs are
    # respected. ClickHouse returns the type's default (0) for a non-Nullable argument
    # and skips NULLs in first_value / last_value. A Nullable argument also lets a
    # NULL or Nullable default through (ClickHouse wants the argument's type).
    if isinstance(node, exp.Lag | exp.Lead | exp.NthValue):
        # Idempotent: a metric inlined into a filter is rewritten again with it.
        if not (isinstance(node.this, exp.Anonymous) and node.this.name == "toNullable"):
            node.set("this", _f("toNullable", node.this))
        return node
    if isinstance(node, exp.FirstValue | exp.LastValue) and not isinstance(
        node.parent, exp.RespectNulls | exp.IgnoreNulls
    ):
        return exp.RespectNulls(this=node)
    if isinstance(node, exp.Contains):
        return exp.GT(
            this=_f("position", node.this, node.expression), expression=exp.Literal.number(0)
        )
    return node


def rewrite_tree(tree: exp.Expression) -> exp.Expression:
    """A copy of ``tree`` with every node rewritten for ClickHouse."""
    tree = tree.copy()
    # Bottom-up, so a rewrite that replaces a node still sees its arguments rewritten;
    # Expression.transform does not descend into a replaced node.
    for node in reversed(list(tree.dfs())):
        parent, arg_key, index = node.parent, node.arg_key, node.index
        new = rewrite(node)
        if new is node:
            continue
        if parent is None:
            tree = new
        else:
            parent.set(arg_key, new, index)
    return tree


def to_clickhouse(expression: str | exp.Expression) -> str:
    """ClickHouse SQL for an Ossie expression."""
    tree = parse(expression) if isinstance(expression, str) else expression
    return rewrite_tree(tree).sql(dialect="clickhouse")


def translate(expression: OssieExpression) -> str:
    """ClickHouse SQL for a model field or metric expression."""
    return to_clickhouse(pick_expression(expression))


def untranslatable(model: OssieDocument) -> tuple[dict[str, str], dict[str, str]]:
    """Fields ("dataset.field") and metrics whose expression does not translate, with why.

    The executor hides them: an expression that does not parse cannot be shown
    to read only columns the user may see."""

    def problem(expression: OssieExpression) -> str | None:
        try:
            translate(expression)
        except ValueError as e:
            return str(e)
        return None

    fields = {
        f"{d.name}.{f.name}": p
        for d in model.datasets
        for f in d.fields or []
        if (p := problem(f.expression))
    }
    metrics = {m.name: p for m in model.metrics or [] if (p := problem(m.expression))}
    return fields, metrics
