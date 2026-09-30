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

from ossie_clickhouse.translate import _rewrite, parse, pick_expression


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
    filters: tuple[str, ...] = ()  # Ossie expressions over dataset.field
    order_by: tuple[str, ...] = ()  # metric or dimension names, "name desc" for descending
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

    def resolve(self, expression: str, used: set[str]) -> exp.Expression:
        """Parse an expression, inline `dataset.field` references, qualify columns.

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
                raise PlanError(f"unqualified column {node.name!r} in {expression!r}")
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
        for r in self.model.relationships or []:
            if r.from_dataset.upper() == root.name.upper() and r.to.upper() == target.name.upper():
                if not self._is_many_to_one(r, target):
                    raise PlanError(
                        f"relationship {r.name!r} is not many-to-one: to_columns "
                        f"{r.to_columns} are not a primary or unique key of {target.name!r}"
                    )
                return r
        raise PlanError(
            f"no direct relationship from {root.name!r} to {target.name!r}; "
            "this version supports one root dataset and its direct relationships"
        )

    @staticmethod
    def _is_many_to_one(r: OssieRelationship, target: OssieDataset) -> bool:
        keys = [target.primary_key or [], *(target.unique_keys or [])]
        cols = {c.upper() for c in r.to_columns}
        return any(cols == {c.upper() for c in k} for k in keys if k)

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
        used: set[str] = set()
        selects: list[exp.Expression] = []
        group: list[exp.Expression] = []
        seen_aliases: set[str] = set()

        for ref in q.dimensions:
            ds, f = self.field_ref(ref)
            used.add(ds.name)
            if f.name in seen_aliases:
                raise PlanError(f"two dimensions named {f.name!r}; use one per query")
            seen_aliases.add(f.name)
            e = self._field_expr(ds, f)
            selects.append(e.as_(f.name))
            group.append(e.copy())

        for name in q.metrics:
            m = self._metrics.get(name.upper())
            if m is None:
                names = (x.name for x in self.model.metrics or [])
                raise PlanError(f"unknown metric {name!r}{_suggest(name, names)}")
            selects.append(self.resolve(pick_expression(m.expression), used).as_(m.name))

        where = []
        for f in q.filters:
            tree = self.resolve(f, used)
            if list(tree.find_all(exp.AggFunc)):
                raise PlanError(f"filters on aggregates are not supported: {f!r}")
            where.append(tree)

        root = self._root(used)
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
        if q.order_by:
            sel = sel.order_by(*self._order(q, selects))
        if q.limit is not None:
            sel = sel.limit(q.limit)
        if dict_keys:
            sel = sel.transform(lambda n: self._dict_get(n, dict_keys))
        # ClickHouse fills unmatched LEFT JOIN columns with defaults unless told otherwise.
        sel.set(
            "settings", [exp.EQ(this=exp.var("join_use_nulls"), expression=exp.Literal.number(1))]
        )
        return sel.transform(_rewrite)

    def _order(self, q: Query, selects: list[exp.Expression]) -> list[exp.Expression]:
        """ORDER BY over selected aliases; names are metrics, dataset.field, or field names."""
        aliases = {e.alias.upper(): e.alias for e in selects}
        for ref in q.dimensions:  # allow the dataset.field spelling too
            aliases[ref.upper()] = ref.split(".")[1]
        out = []
        for item in q.order_by:
            name, _, direction = item.partition(" ")
            desc = direction.strip().upper() == "DESC"
            if direction and not desc and direction.strip().upper() != "ASC":
                raise PlanError(f"order_by item {item!r}: use 'name', 'name asc' or 'name desc'")
            alias = aliases.get(name.upper())
            if alias is None:
                raise PlanError(
                    f"order_by {name!r} is not a selected metric or dimension"
                    f"{_suggest(name, aliases.values())}"
                )
            out.append(exp.Ordered(this=exp.column(alias), desc=desc, nulls_first=not desc))
        return out

    def _table(self, ds: OssieDataset) -> exp.Expression:
        table = source_table(ds.source).as_(ds.name)
        info = (self.catalog or {}).get(ds.name)
        return exp.Final(this=table) if info and info.dedup else table

    def _dict_get(self, node: exp.Expression, dict_keys: dict[str, exp.Expression]):
        """dataset.column on a dictionary dataset -> dictGetOrNull('db.dict', 'column', key)."""
        if isinstance(node, exp.Column) and node.table in dict_keys:
            ds = self.dataset(node.table)
            return exp.Anonymous(
                this="dictGetOrNull",
                expressions=[
                    exp.Literal.string(source_table(ds.source).sql()),
                    exp.Literal.string(node.name),
                    dict_keys[node.table].copy(),
                ],
            )
        return node

    def sql(self, q: Query, pretty: bool = False) -> str:
        return self.plan(q).sql(dialect="clickhouse", pretty=pretty)
