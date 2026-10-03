"""Turn a semantic query (metrics, dimensions, filters) into one ClickHouse SELECT.

Minimum planner: one root dataset plus direct many-to-one joins to the
datasets the query touches. Anything else is rejected with a message that
names the nearest supported thing.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from ossie import OssieDataset, OssieDocument, OssieField, OssieRelationship
from sqlglot import exp

from ossie_clickhouse.model import join_problem
from ossie_clickhouse.translate import parse, pick_expression, rewrite_tree


class PlanError(ValueError):
    pass


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
    """Ossie source (`db.schema.table`, `db.table`, `table`) to a ClickHouse table."""
    if any(ch.isspace() for ch in source.strip()) or "(" in source:
        raise PlanError(f"query sources are not supported yet: {source!r}")
    parts = source.split(".")
    if len(parts) > 3:
        raise PlanError(f"cannot map source {source!r} to a ClickHouse table")
    # ClickHouse has no schema level: keep database and table, drop the middle.
    return exp.table_(parts[-1], db=parts[0] if len(parts) > 1 else None)


# Aggregates whose value does not change when an input row repeats. Only these
# may read a joined dataset's columns on their own: each row of a many-to-one
# target repeats once per root row that references it.
_REPEAT_SAFE = (exp.Min, exp.Max, exp.AnyValue, exp.LogicalAnd, exp.LogicalOr, exp.ApproxDistinct)


def _repeat_safe(agg: exp.AggFunc) -> bool:
    return isinstance(agg, _REPEAT_SAFE) or isinstance(agg.this, exp.Distinct)


def _aggregate_inputs(tree: exp.Expression) -> list[tuple[exp.AggFunc, set[str]]]:
    """Each aggregate that reads joined rows, with the datasets of the columns it reads.

    A column counts for its nearest aggregate only, so SUM(SUM(x)) OVER () reads x
    once. The function of an OVER clause reads grouped rows, not joined ones, and
    PARTITION BY / ORDER BY columns are grouping keys: neither counts."""
    found: dict[int, tuple[exp.AggFunc, set[str]]] = {}
    for col in tree.find_all(exp.Column):
        node = col.parent
        while node is not None and not (
            isinstance(node, exp.AggFunc) and not _window_function(node)
        ):
            node = node.parent
        if node is not None:
            found.setdefault(id(node), (node, set()))[1].add(col.table)
    return list(found.values())


def _window_function(agg: exp.AggFunc) -> bool:
    parent = agg.parent
    if isinstance(parent, exp.RespectNulls | exp.IgnoreNulls):
        parent = parent.parent
    return isinstance(parent, exp.Window)


def _repeated(tree: exp.Expression, root: str) -> tuple[exp.AggFunc, set[str]] | None:
    """A duplicate-sensitive aggregate that reads only datasets other than ``root``."""
    for agg, datasets in _aggregate_inputs(tree):
        if root not in datasets and not _repeat_safe(agg):
            return agg, datasets
    return None


def unanswerable_metrics(model: OssieDocument) -> dict[str, str]:
    """Metrics the planner refuses in every question, with why.

    A duplicate-sensitive aggregate over each of two datasets: whichever is
    the root, the other one's rows repeat."""
    planner = Planner(model)
    out = {}
    for m in model.metrics or []:
        try:
            # Through the planner, so names are the model's spelling, as in plan().
            tree = rewrite_tree(planner.resolve(pick_expression(m.expression), set()))
        except ValueError:
            continue  # untranslatable, reported as such
        homes = sorted({d for _, ds in _aggregate_inputs(tree) for d in ds})
        if homes and all(_repeated(tree, root) for root in homes):
            aggs = ", ".join(_repeated(tree, root)[0].sql("clickhouse") for root in homes)
            out[m.name] = (
                f"aggregates columns of {' and '.join(homes)} separately with functions that "
                f"count repeated rows ({aggs}); whichever "
                "is the root of a question, the other's rows repeat, so every question "
                "with it is refused"
            )
    return out


def _check_rows(label: str, tree: exp.Expression, root: str, touched: dict[str, set[str]]) -> None:
    """Refuse an aggregate expression whose answer would be about other rows than the root's.

    Joins are many-to-one, so a joined dataset's rows repeat per root row
    and include only those the root references."""
    homes = sorted({d for _, ds in _aggregate_inputs(tree) for d in ds})
    if homes and root not in homes:
        by = [item for item, ds in touched.items() if root in ds and item != label] or [label]
        home = " and ".join(homes)
        raise PlanError(
            f"{label} aggregates {home} rows, but this question is answered over "
            f"{root} rows (because of {', '.join(by)}): it would count only {home} "
            f"rows that {root} references, once per reference. Ask for metrics of "
            f"different datasets in separate questions, and filter or group a {home} "
            f"metric only by fields of {home} or of datasets {home} joins to"
        )
    repeated = _repeated(tree, root)
    if repeated:
        agg, joined = repeated[0].sql("clickhouse"), " and ".join(sorted(repeated[1]))
        raise PlanError(
            f"{label} applies {agg} to {joined} columns alone over {root} rows, "
            f"so each {joined} row would count once per {root} row that references it. "
            "Only MIN, MAX, ANY_VALUE, BOOL_AND, BOOL_OR and DISTINCT aggregates may "
            "read a joined dataset on their own; this version cannot answer it"
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
                    return self.resolve(pick_expression(self.metric(node.name).expression), used)
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

    # --- assembly -----------------------------------------------------------

    def plan(self, q: Query) -> exp.Select:
        if not q.metrics and not q.dimensions:
            raise PlanError("a query needs at least one metric or dimension")
        # Query item ("metric 'x'") -> datasets it reads: picks the root, explains it.
        touched: dict[str, set[str]] = {}
        checked: list[tuple[str, exp.Expression]] = []  # aggregate expressions, rewritten
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
            e = self._field_expr(ds, f)
            selects.append(e.as_(alias(f.name)))
            group.append(e.copy())

        for name in q.metrics:
            m = self.metric(name)
            label = f"metric {m.name!r}"
            tree = self.resolve(pick_expression(m.expression), touched.setdefault(label, set()))
            checked.append((label, rewrite_tree(tree)))
            selects.append(tree.as_(alias(m.name)))

        where: list[exp.Expression] = []
        having: list[exp.Expression] = []  # filters over aggregates or metric names
        # Rewrite before inspecting: APPROX_PERCENTILE is a plain function name to
        # SQLGlot and an aggregate only after rewrite. rewrite is idempotent, so
        # the final pass over the whole SELECT leaves these nodes as they are.
        grouped = {rewrite_tree(e).sql() for e in group}
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
            checked.append((label, tree))

        used = set().union(*touched.values())
        root = self._root(used)
        for label, tree in checked:
            _check_rows(label, tree, root.name, touched)
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
        return rewrite_tree(sel)

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
            name = exp.Literal.string(source_table(ds.source).sql())
            key = dict_keys[node.table].copy()
            if node.name.upper() == (self.catalog[ds.name].dictionary_key or "").upper():
                has = exp.Anonymous(this="dictHas", expressions=[name, key])
                return exp.If(this=has, true=key.copy(), false=exp.Null())
            return exp.Anonymous(
                this="dictGetOrNull", expressions=[name, exp.Literal.string(node.name), key]
            )
        return node

    def sql(self, q: Query, pretty: bool = False) -> str:
        return self.plan(q).sql(dialect="clickhouse", pretty=pretty)
