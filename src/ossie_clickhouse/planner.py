"""Turn a semantic query (metrics, dimensions, filters) into one ClickHouse SELECT.

Minimum planner: one root dataset plus direct many-to-one joins to the
datasets the query touches. Anything else is rejected with a message that
names the nearest supported thing.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field, replace
from functools import cache

from ossie import OssieDataset, OssieDocument, OssieField, OssieRelationship
from sqlglot import exp

from ossie_clickhouse.model import covers_key, join_problem
from ossie_clickhouse.translate import (
    CLICKHOUSE,
    parse,
    pick_expression,
    rewrite_tree,
    untranslatable,
)


class PlanError(ValueError):
    pass


_QUOTED = re.compile(r"`(?:[^`]|``)*`|\"(?:[^\"]|\"\")*\"")
# A non-empty quoted identifier, or unquoted text without a quote, space, `;`,
# parenthesis or dot.
_PART = re.compile(r"`(?:[^`]|``)+`|\"(?:[^\"]|\"\")+\"|[^\s`\"'();.]+")
_SOURCE = re.compile(rf"(?:{_PART.pattern})(?:\.(?:{_PART.pattern})){{0,2}}")


@dataclass(frozen=True)
class TableInfo:
    """What the executor learned about a dataset's physical source."""

    engine: str
    columns: frozenset[str]
    dedup: bool = False  # ReplacingMergeTree and not overridden: read with FINAL
    dictionary_key: str | None = None  # single-key dictionary: read with dictGetOrNull


Catalog = dict[str, TableInfo]  # by dataset name


@dataclass(frozen=True)
class Query:
    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()  # "dataset.field"
    # Ossie expressions over dataset.field (WHERE) or over metric names and
    # aggregates (HAVING); window metrics cannot be filtered in one SELECT.
    filters: tuple[str, ...] = ()
    # Metric or dimension names, "name desc" for descending. Empty with metrics
    # and dimensions: first metric descending, so callers that rank rows
    # themselves (agents do, and get it wrong) see the top rows first.
    order_by: tuple[str, ...] = ()
    limit: int | None = None


def source_table(source: str) -> exp.Table:
    """Ossie source (`db.schema.table`, `db.table`, `table`) to a ClickHouse table.

    Parts may be quoted with backticks or double quotes; `.db` and `.name` of
    the result are unquoted, and the emitted SQL keeps the quotes."""
    parts = [exp.to_identifier(name, quoted=quoted or None) for name, quoted in _parts(source)]
    # ClickHouse has no schema level: keep database and table, drop the middle.
    return exp.table_(parts[-1], db=parts[0] if len(parts) > 1 else None)


@cache
def _parts(source: str) -> tuple[tuple[str, bool], ...]:
    """Each dot-separated part of a source as (name, quoted).

    A part is one quoted identifier (a doubled quote stands for itself) or
    unquoted text taken as the name it spells, as 0.2.3 did: `t$x`, `t#1`,
    `select`, `x-y`. Whitespace or a parenthesis outside quotes marks a query.
    Refused rather than read as some other table: `--` or `/*` outside quotes
    (a comment pasted in), `;` or an unclosed quote, and `\\` anywhere: in a
    quoted name ClickHouse reads it as an escape, which is not decoded here."""
    s = source.strip()
    bare = _QUOTED.sub("_", s)
    if "\\" in s or re.search(r"--|/\*|;|[`\"]", bare):
        raise PlanError(f"cannot map source {source!r} to a ClickHouse table")
    if re.search(r"[\s()]", bare):
        raise PlanError(f"query sources are not supported yet: {source!r}")
    if not _SOURCE.fullmatch(s):
        raise PlanError(f"cannot map source {source!r} to a ClickHouse table")
    return tuple(
        (p[1:-1].replace(p[0] * 2, p[0]), True) if p[0] in '`"' else (p, False)
        for p in _PART.findall(s)
    )


def source_name(source: str) -> str:
    """`db.table` unquoted, as dictGet* takes a dictionary's name."""
    t = source_table(source)
    return f"{t.db}.{t.name}" if t.db else t.name


# Aggregates whose value does not change when an input row repeats. Only these
# may read a joined dataset's columns on their own: each row of a many-to-one
# target repeats once per root row that references it.
_REPEAT_SAFE = {
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.AnyValue: "ANY_VALUE",
    exp.LogicalAnd: "BOOL_AND",
    exp.LogicalOr: "BOOL_OR",
    exp.ApproxDistinct: "APPROX_COUNT_DISTINCT",
}
_REPEAT_SAFE_NAMES = ", ".join(_REPEAT_SAFE.values()) + " and DISTINCT"

Inputs = list[tuple[exp.AggFunc, set[str]]]


def _repeat_safe(agg: exp.AggFunc) -> bool:
    return isinstance(agg, tuple(_REPEAT_SAFE)) or isinstance(agg.this, exp.Distinct)


def _aggregate_inputs(tree: exp.Expression) -> Inputs:
    """Each aggregate over joined rows, with the datasets of the columns it reads
    (none for COUNT(*)).

    A column counts for its nearest aggregate only, so SUM(SUM(x)) OVER () reads x
    once, and a FILTER (WHERE ...) column for the aggregate it filters. The function
    of an OVER clause reads grouped rows, not joined ones, and PARTITION BY / ORDER BY
    columns are grouping keys: neither counts."""
    found = {id(a): (a, set()) for a in tree.find_all(exp.AggFunc) if not _window_function(a)}
    for col in tree.find_all(exp.Column):
        node = col.parent
        while node is not None and id(node) not in found:
            filtered = isinstance(node, exp.Filter) and id(node.this) in found
            node = node.this if filtered else node.parent
        if node is not None:
            found[id(node)][1].add(col.table)
    return list(found.values())


def _window_function(agg: exp.AggFunc) -> bool:
    parent = agg.parent
    while isinstance(parent, exp.RespectNulls | exp.IgnoreNulls | exp.Filter):
        parent = parent.parent
    return isinstance(parent, exp.Window)


def _homes(inputs: Inputs) -> list[str]:
    return sorted({d for _, ds in inputs for d in ds})


def _repeated(inputs: Inputs, root: str, one_to_one: set[str]) -> Inputs:
    """Duplicate-sensitive aggregates that read only datasets whose rows repeat over ``root``'s."""
    return [
        (agg, ds)
        for agg, ds in inputs
        if ds and root not in ds and not ds <= one_to_one and not _repeat_safe(agg)
    ]


def unanswerable_metrics(model: OssieDocument) -> dict[str, str]:
    """Metrics the planner refuses in every question, with why.

    One that names an unknown field or reads no column, and one with a
    duplicate-sensitive aggregate over each of two datasets: whichever is the
    root, the other one's rows repeat. Untranslatable metrics are reported by
    untranslatable()."""
    planner = Planner(model)
    skip = set(untranslatable(model)[1])
    out = {}
    for m in model.metrics or []:
        if m.name in skip:
            continue
        try:
            # Through the planner, so names are the model's spelling, as in plan().
            inputs = _aggregate_inputs(planner._metric_tree(m, set()))
        except PlanError as e:
            out[m.name] = str(e)
            continue
        except ValueError:
            continue  # reads an untranslatable field, reported as such
        homes = _homes(inputs)
        refused = [_repeated(inputs, root, planner._one_to_one(root)) for root in homes]
        if homes and all(refused):
            aggs = ", ".join(r[0][0].sql(CLICKHOUSE) for r in refused)
            out[m.name] = (
                f"aggregates columns of {' and '.join(homes)} separately with functions that "
                f"count repeated rows ({aggs}); whichever "
                "is the root of a question, the other's rows repeat, so every question "
                "with it is refused"
            )
    return out


def _check_rows(
    label: str, inputs: Inputs, root: str, touched: dict[str, set[str]], one_to_one: set[str]
) -> None:
    """Refuse an aggregate expression whose answer would be about other rows than the root's.

    Joins are many-to-one, so a joined dataset's rows repeat per root row
    and include only those the root references."""
    homes = _homes(inputs)
    repeated = _repeated(inputs, root, one_to_one)
    if homes and root not in homes:
        by = [item for item, ds in touched.items() if root in ds and item != label] or [label]
        home = " and ".join(homes)
        raise PlanError(
            f"{label} aggregates {home} rows, but this question is answered over "
            f"{root} rows (because of {', '.join(by)}): it would see only {home} "
            f"rows that {root} references{', once per reference' if repeated else ''}. "
            f"Filter or group a {home} metric only by fields of {home} or of datasets "
            f"{home} joins to"
        )
    if repeated:
        agg, joined = repeated[0][0].sql(CLICKHOUSE), " and ".join(sorted(repeated[0][1]))
        raise PlanError(
            f"{label} applies {agg} to {joined} columns alone over {root} rows, "
            f"so each {joined} row would count once per {root} row that references it. "
            f"Only {_REPEAT_SAFE_NAMES} aggregates may read a joined dataset on their own, "
            f"unless it is joined on a key of {root} (one row each); "
            "this version cannot answer it"
        )


def _suggest(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=3, cutoff=0.5)
    return f" (did you mean: {', '.join(close)}?)" if close else ""


@dataclass
class Planner:
    model: OssieDocument
    catalog: Catalog | None = None  # from Executor.introspect(); None means plain tables
    _datasets: dict[str, OssieDataset] = field(init=False)
    _fields: dict[str, dict[str, OssieField]] = field(init=False)
    _metrics: dict[str, object] = field(init=False)

    def __post_init__(self):
        # Spec: unquoted identifiers are case-insensitive; index by upper case,
        # but always emit the physical names as written in the model.
        self._datasets = {d.name.upper(): d for d in self.model.datasets}
        self._fields = {
            d.name.upper(): {f.name.upper(): f for f in d.fields or []} for d in self.model.datasets
        }
        self._metrics = {m.name.upper(): m for m in self.model.metrics or []}

    # --- resolution ---------------------------------------------------------

    def dataset(self, name: str) -> OssieDataset:
        ds = self._datasets.get(name.upper())
        if ds is None:
            raise PlanError(
                f"unknown dataset {name!r}{_suggest(name, (d.name for d in self.model.datasets))}"
            )
        return ds

    def metric(self, name: str):
        m = self._metrics.get(name.upper())
        if m is None:
            names = (x.name for x in self.model.metrics or [])
            raise PlanError(f"unknown metric {name!r}{_suggest(name, names)}")
        return m

    def field_ref(self, ref: str) -> tuple[OssieDataset, OssieField]:
        if ref.count(".") != 1:
            raise PlanError(f"field reference must be dataset.field, got {ref!r}")
        ds_name, f_name = ref.split(".")
        ds = self.dataset(ds_name)
        f = self._fields[ds.name.upper()].get(f_name.upper())
        if f is None:
            names = (f"{ds.name}.{x.name}" for x in ds.fields or [])
            raise PlanError(f"unknown field {ref!r}{_suggest(ref, names)}")
        return ds, f

    def resolve(self, expression: str, used: set[str], metrics: bool = False) -> exp.Expression:
        """Parse an expression, inline `dataset.field` references, qualify columns.

        With ``metrics``, a bare name that is a metric inlines that metric's
        expression (filters may say ``total_sales > 1000``).
        Records the datasets touched in ``used`` (by model name).
        """
        try:
            tree = parse(expression)
        except ValueError as e:
            raise PlanError(str(e)) from e

        def inline(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            if not node.table:
                if metrics and node.name.upper() in self._metrics:
                    return self._metric_tree(self.metric(node.name), used)
                hint = ""
                if metrics:
                    names = [x.name for x in self.model.metrics or []] + [
                        f"{d.name}.{x.name}" for d in self.model.datasets for x in d.fields or []
                    ]
                    hint = f"; use dataset.field or a metric name{_suggest(node.name, names)}"
                raise PlanError(f"unqualified column {node.name!r} in {expression!r}{hint}")
            ds, f = self.field_ref(f"{node.table}.{node.name}")
            used.add(ds.name)
            return self._field_expr(ds, f)

        return tree.transform(inline)

    def _metric_tree(self, m, used: set[str]) -> exp.Expression:
        """A metric's expression, resolved and rewritten for ClickHouse.

        Refuses an aggregate that reads no column (COUNT(*)) unless the metric's
        other aggregates name the one dataset whose rows it counts: otherwise it
        would count rows of whichever dataset a question is answered over."""
        tree = rewrite_tree(self.resolve(pick_expression(m.expression), used))
        if len(self.model.datasets) > 1:
            inputs = _aggregate_inputs(tree)
            unnamed = [agg for agg, ds in inputs if not ds]
            if unnamed and len(_homes(inputs)) != 1:
                raise PlanError(
                    f"metric {m.name!r} uses {unnamed[0].sql(CLICKHOUSE)}, which reads no "
                    "column, so it would count rows of whichever dataset a question is "
                    "answered over; count a column of the dataset it means, such as "
                    "COUNT(dataset.key)"
                )
        return tree

    def _field_expr(self, ds: OssieDataset, f: OssieField) -> exp.Expression:
        """A field's expression with its bare columns qualified by the dataset alias."""
        tree = parse(pick_expression(f.expression))

        def qualify(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column) and not node.table:
                return exp.column(node.this, table=ds.name)
            if isinstance(node, exp.Column):
                raise PlanError(
                    f"field {ds.name}.{f.name} references another dataset ({node.sql()}); "
                    "fields may only use their own columns"
                )
            return node

        return tree.transform(qualify)

    # --- joins --------------------------------------------------------------

    def _relationship(self, root: OssieDataset, target: OssieDataset) -> OssieRelationship:
        found = [
            r
            for r in self.model.relationships or []
            if r.from_dataset.upper() == root.name.upper() and r.to.upper() == target.name.upper()
        ]
        if len(found) > 1:  # _root chose a dataset related to every other one
            raise PlanError(
                f"ambiguous join from {root.name!r} to {target.name!r}: relationships "
                f"{[r.name for r in found]}; this version cannot choose between them"
            )
        r = found[0]
        problem = join_problem(r, target)
        if problem:
            raise PlanError(problem)
        return r

    def _one_to_one(self, root: str) -> set[str]:
        """Datasets joined from ``root`` on a key of ``root``: none of their rows repeats."""
        found: dict[str, list[OssieRelationship]] = {}
        for r in self.model.relationships or []:
            if r.from_dataset.upper() == root.upper() and r.to.upper() in self._datasets:
                found.setdefault(r.to.upper(), []).append(r)
        ds = self.dataset(root)
        return {
            self._datasets[to].name
            for to, rs in found.items()
            if len(rs) == 1 and covers_key(rs[0].from_columns, ds)
        }

    def _root(self, used: set[str]) -> OssieDataset:
        """The dataset that has direct relationships to every other used dataset."""
        candidates = []
        for name in sorted(used):
            ds = self.dataset(name)
            others = used - {name}
            rels = {
                r.to.upper()
                for r in self.model.relationships or []
                if r.from_dataset.upper() == ds.name.upper()
            }
            if all(o.upper() in rels for o in others):
                candidates.append(ds)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise PlanError(
                f"datasets {sorted(used)} are not joined by direct relationships from one root"
            )
        raise PlanError(f"ambiguous root dataset among {[c.name for c in candidates]}")

    def _split_hint(self, q: Query) -> str:
        """Advice to ask each metric in its own question, given only when each one plans."""
        if len(q.metrics) < 2:
            return ""
        try:
            for m in q.metrics:
                self.plan(replace(q, metrics=(m,), order_by=(), limit=None))
        except PlanError:
            return ""
        if len(q.dimensions) != 1:  # one IN list cannot pick pairs of values
            return ". Ask for each metric in its own question"
        d = q.dimensions[0]
        return (
            ". Ask for each metric in its own question; for the others over the rows of "
            f'the first answer, such as its top 5, filter them with "{d} IN (...)" listing '
            f'the values it returned, or with the single filter "{d} IN (...) OR {d} IS NULL" '
            "if one is NULL"
        )

    # --- assembly -----------------------------------------------------------

    def plan(self, q: Query) -> exp.Select:
        if not q.metrics and not q.dimensions:
            raise PlanError("a query needs at least one metric or dimension")
        # Query item ("metric 'x'") -> datasets it reads: picks the root, explains it.
        touched: dict[str, set[str]] = {}
        checked: list[tuple[str, Inputs]] = []  # aggregate expressions
        selects: list[exp.Expression] = []
        group: list[exp.Expression] = []
        seen_aliases: set[str] = set()  # upper-cased: ORDER BY resolves names case-insensitively

        def alias(name: str) -> str:
            if name.upper() in seen_aliases:
                raise PlanError(
                    f"two selected columns named {name!r}: a metric and a dimension, or "
                    "fields of two datasets, cannot share a name in one query"
                )
            seen_aliases.add(name.upper())
            return name

        for ref in q.dimensions:
            ds, f = self.field_ref(ref)
            touched.setdefault(f"dimension {ref!r}", set()).add(ds.name)
            e = rewrite_tree(self._field_expr(ds, f))
            selects.append(e.as_(alias(f.name)))
            group.append(e.copy())

        for name in q.metrics:
            m = self.metric(name)
            label = f"metric {m.name!r}"
            tree = self._metric_tree(m, touched.setdefault(label, set()))
            checked.append((label, _aggregate_inputs(tree)))
            selects.append(tree.as_(alias(m.name)))

        where: list[exp.Expression] = []
        having: list[exp.Expression] = []  # filters over aggregates or metric names
        # Every part is rewritten once, before inspecting: APPROX_PERCENTILE is a
        # plain function name to SQLGlot and an aggregate only after rewrite.
        grouped = {e.sql() for e in group}
        for f in q.filters:
            label = f"filter {f!r}"
            tree = self.resolve(f, touched.setdefault(label, set()), metrics=True)
            tree = rewrite_tree(tree)
            if tree.find(exp.Window):
                raise PlanError(
                    f"filter {f!r} uses a window function; a window metric cannot be "
                    "filtered in the same SELECT, select it and filter the rows instead"
                )
            if not tree.find(exp.AggFunc):
                where.append(tree)
                continue
            # HAVING may only see aggregates and GROUP BY expressions.
            for col in tree.find_all(exp.Column):
                if col.find_ancestor(exp.AggFunc):
                    continue
                node: exp.Expression | None = col
                while node is not None and node.sql() not in grouped:
                    node = node.parent
                if node is None:
                    raise PlanError(
                        f"filter {f!r} mixes an aggregate with {col.sql()}, which is not a "
                        "dimension of this query; add it to dimensions or put it in its own filter"
                    )
            having.append(tree)
            checked.append((label, _aggregate_inputs(tree)))

        used = set().union(*touched.values())
        try:
            root = self._root(used)
            one_to_one = self._one_to_one(root.name)
            for label, inputs in checked:
                _check_rows(label, inputs, root.name, touched, one_to_one)
        except PlanError as e:
            raise PlanError(f"{e}{self._split_hint(q)}") from None
        sel = exp.select(*selects).from_(self._table(root))
        dict_keys: dict[str, exp.Expression] = {}  # dictionary dataset -> key expression
        for name in sorted(used - {root.name}):
            target = self.dataset(name)
            r = self._relationship(root, target)
            info = (self.catalog or {}).get(target.name)
            if (
                info
                and info.dictionary_key
                and [c.upper() for c in r.to_columns] == [info.dictionary_key.upper()]
            ):
                dict_keys[target.name] = exp.column(r.from_columns[0], table=root.name)
                continue
            on = exp.and_(
                *(
                    exp.EQ(
                        this=exp.column(a, table=root.name),
                        expression=exp.column(b, table=target.name),
                    )
                    for a, b in zip(r.from_columns, r.to_columns, strict=True)
                )
            )
            sel = sel.join(self._table(target), on=on, join_type="left")
        if where:
            sel = sel.where(exp.and_(*where))
        if group:
            sel = sel.group_by(*group)
        if having:
            sel = sel.having(exp.and_(*having))
        order_by = q.order_by or ((f"{q.metrics[0]} desc",) if q.metrics and group else ())
        if order_by:
            sel = sel.order_by(*self._order(order_by, q, selects))
        if q.limit is not None:
            sel = sel.limit(q.limit)
        if dict_keys:
            sel = sel.transform(lambda n: self._dict_get(n, dict_keys))
        # ClickHouse fills unmatched LEFT JOIN columns with defaults unless told otherwise.
        sel.set(
            "settings", [exp.EQ(this=exp.var("join_use_nulls"), expression=exp.Literal.number(1))]
        )
        return sel

    def _order(
        self, order_by: tuple[str, ...], q: Query, selects: list[exp.Expression]
    ) -> list[exp.Expression]:
        """ORDER BY over selected aliases; names are metrics, dataset.field, or field names."""
        aliases = {e.alias.upper(): e.alias for e in selects}
        for ref in q.dimensions:  # allow the dataset.field spelling too
            aliases[ref.upper()] = self.field_ref(ref)[1].name
        out = []
        for item in order_by:
            name, _, direction = item.partition(" ")
            desc = direction.strip().upper() == "DESC"
            if direction and not desc and direction.strip().upper() != "ASC":
                raise PlanError(f"order_by item {item!r}: use 'name', 'name asc' or 'name desc'")
            alias = aliases.get(name.upper())
            if alias is None:
                raise PlanError(
                    f"order_by {name!r} is not a selected metric or dimension"
                    f"{_suggest(name, dict.fromkeys(aliases.values()))}"
                )
            out.append(exp.Ordered(this=exp.column(alias), desc=desc, nulls_first=False))
        return out

    def _table(self, ds: OssieDataset) -> exp.Expression:
        table = source_table(ds.source).as_(ds.name)
        info = (self.catalog or {}).get(ds.name)
        return exp.Final(this=table) if info and info.dedup else table

    def _dict_get(self, node: exp.Expression, dict_keys: dict[str, exp.Expression]):
        """dataset.column on a dictionary dataset -> dictGetOrNull('db.dict', 'column', key).

        The key column is not an attribute, so dictGet* cannot read it: emit the
        key itself, NULL when the dictionary lacks it, as a LEFT JOIN would."""
        if isinstance(node, exp.Column) and node.table in dict_keys:
            ds = self.dataset(node.table)
            name = exp.Literal.string(source_name(ds.source))
            key = dict_keys[node.table].copy()
            if node.name.upper() == (self.catalog[ds.name].dictionary_key or "").upper():
                has = exp.Anonymous(this="dictHas", expressions=[name, key])
                return exp.If(this=has, true=key.copy(), false=exp.Null())
            return exp.Anonymous(
                this="dictGetOrNull", expressions=[name, exp.Literal.string(node.name), key]
            )
        return node

    def sql(self, q: Query, pretty: bool = False) -> str:
        return self.plan(q).sql(dialect=CLICKHOUSE, pretty=pretty)
