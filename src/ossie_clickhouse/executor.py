"""Run semantic queries against ClickHouse.

Learns what it needs from the server (`system.tables`, `system.columns`,
`system.dictionaries`) rather than from the user, and lets a dataset's
`custom_extensions` entry with `vendor_name: CLICKHOUSE` override it.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

import clickhouse_connect
from ossie import OssieDataset, OssieDocument
from sqlglot import exp

from ossie_clickhouse.planner import Catalog, PlanError, Planner, Query, TableInfo, source_table
from ossie_clickhouse.translate import parse, pick_expression

DEFAULT_URL = "http://127.0.0.1:8123"
VENDOR = "CLICKHOUSE"


def connect(url: str | None = None):
    return clickhouse_connect.get_client(
        dsn=url or os.environ.get("OSSIE_CLICKHOUSE_URL", DEFAULT_URL)
    )


def overrides(ds: OssieDataset) -> dict:
    """Our `custom_extensions` entry for a dataset, parsed. Keys: dedup ("final" | "none")."""
    for ext in ds.custom_extensions or []:
        if ext.vendor_name.upper() == VENDOR:
            return json.loads(ext.data)
    return {}


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    sql: str


class Executor:
    def __init__(self, client, model: OssieDocument):
        self.client = client
        self.model = model
        self.catalog = self.introspect()
        self.planner = Planner(model, self.catalog)

    # --- introspection ------------------------------------------------------

    def introspect(self) -> Catalog:
        tables: dict[
            tuple[str, str], list[OssieDataset]
        ] = {}  # several datasets may share a source
        for ds in self.model.datasets:
            try:
                t = source_table(ds.source)
            except PlanError:
                continue
            tables.setdefault((t.db or self.client.database or "default", t.name), []).append(ds)
        if not tables:
            return {}
        pairs = ", ".join(f"('{db}', '{name}')" for db, name in tables)
        rows = self.client.query(
            "SELECT database, name, engine, engine_full FROM system.tables "
            f"WHERE (database, name) IN ({pairs})"
        ).result_rows
        cols = self.client.query(
            "SELECT database, table, groupArray(name) FROM system.columns "
            f"WHERE (database, table) IN ({pairs}) GROUP BY database, table"
        ).result_rows
        columns = {(db, t): frozenset(c) for db, t, c in cols}
        dicts = self.client.query(
            "SELECT database, name, attribute.names FROM system.dictionaries "
            f"WHERE (database, name) IN ({pairs})"
        ).result_rows
        attributes = {(db, n): set(a) for db, n, a in dicts}

        catalog: Catalog = {}
        for db, name, engine, _full in rows:
            key = None
            if engine == "Dictionary":
                keys = columns[(db, name)] - attributes.get((db, name), set())
                key = next(iter(keys)) if len(keys) == 1 else None
            for ds in tables[(db, name)]:
                catalog[ds.name] = TableInfo(
                    engine=engine,
                    columns=columns.get((db, name), frozenset()),
                    dedup=engine.startswith("Replacing")
                    and overrides(ds).get("dedup", "final") != "none",
                    dictionary_key=key,
                )
        return catalog

    # --- checks -------------------------------------------------------------

    def check(self) -> list[str]:
        """Problems that only the database can reveal: missing sources and columns."""
        problems = []
        for ds in self.model.datasets:
            info = self.catalog.get(ds.name)
            if info is None:
                problems.append(
                    f"dataset {ds.name!r}: source {ds.source!r} not found in ClickHouse"
                )
                continue
            needed: dict[str, str] = {}
            for key in [ds.primary_key or [], *(ds.unique_keys or [])]:
                needed.update({c: "key" for c in key})
            for f in ds.fields or []:
                for c in parse(pick_expression(f.expression)).find_all(exp.Column):
                    if not c.table:
                        needed[c.name] = f"field {f.name!r}"
            for col, where in needed.items():
                if col not in info.columns:
                    problems.append(
                        f"dataset {ds.name!r}: column {col!r} ({where}) not in {ds.source}"
                    )
        for r in self.model.relationships or []:
            for ds_name, cols in ((r.from_dataset, r.from_columns), (r.to, r.to_columns)):
                info = self.catalog.get(ds_name)
                for c in cols:
                    if info and c not in info.columns:
                        problems.append(
                            f"relationship {r.name!r}: column {c!r} not in dataset {ds_name!r}"
                        )
        return problems

    # --- execution ----------------------------------------------------------

    def execute(self, q: Query) -> Result:
        sql = self.planner.sql(q)
        res = self.client.query(sql)
        rows = [tuple(_nan_to_none(v) for v in row) for row in res.result_rows]
        return Result(list(res.column_names), rows, sql)


def _nan_to_none(v):
    return None if isinstance(v, float) and math.isnan(v) else v
