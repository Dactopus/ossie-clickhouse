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

from ossie_clickhouse.access import Hidden, Policy, restrict
from ossie_clickhouse.planner import Catalog, PlanError, Planner, Query, TableInfo, source_table
from ossie_clickhouse.translate import parse, pick_expression, untranslatable

DEFAULT_URL = "http://127.0.0.1:8123"
VENDOR = "CLICKHOUSE"


def connect(url: str | None = None):
    # No session: nothing here needs one, and a session serializes queries, so
    # concurrent MCP tool calls on one client would fail.
    return clickhouse_connect.get_client(
        dsn=url or os.environ.get("OSSIE_CLICKHOUSE_URL", DEFAULT_URL),
        autogenerate_session_id=False,
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
    """Runs as whoever the client is connected as; sees only what that user may read."""

    def __init__(self, client, model: OssieDocument, policy: Policy | None = None):
        self.client = client
        self.full_model = model
        self.untranslatable_fields, untranslatable_metrics = untranslatable(model)
        self.catalog = self.introspect()
        self.user, self.roles = self.client.query(
            "SELECT currentUser(), enabledRoles()"
        ).result_rows[0]
        hidden = self._hidden_by_grants() | Hidden(
            fields=frozenset(self.untranslatable_fields),
            metrics=frozenset(untranslatable_metrics),
        )
        if policy:
            hidden |= policy.hidden_for(self.user, list(self.roles))
        self.model = restrict(model, hidden)
        self.planner = Planner(self.model, self.catalog)

    def _hidden_by_grants(self) -> Hidden:
        """ClickHouse lists in system.tables and system.columns only what the
        connected user may read, so anything the model names and the catalog
        lacks is invisible to this user."""
        datasets, fields, relationships = set(), set(), set()
        for ds in self.full_model.datasets:
            info = self.catalog.get(ds.name)
            if info is None:
                datasets.add(ds.name)
                continue
            for f in ds.fields or []:
                if f"{ds.name}.{f.name}" in self.untranslatable_fields:
                    continue  # hidden anyway
                cols = {c.name for c in parse(pick_expression(f.expression)).find_all(exp.Column)}
                if not cols <= info.columns:
                    fields.add(f"{ds.name}.{f.name}")
        for r in self.full_model.relationships or []:
            for ds_name, cols in ((r.from_dataset, r.from_columns), (r.to, r.to_columns)):
                info = self.catalog.get(ds_name)
                if info and not set(cols) <= info.columns:
                    relationships.add(r.name)
        return Hidden(
            frozenset(datasets), frozenset(fields), relationships=frozenset(relationships)
        )

    # --- introspection ------------------------------------------------------

    def introspect(self) -> Catalog:
        tables: dict[
            tuple[str, str], list[OssieDataset]
        ] = {}  # several datasets may share a source
        for ds in self.full_model.datasets:
            try:
                t = source_table(ds.source)
            except PlanError:
                continue
            tables.setdefault((t.db or self.client.database or "default", t.name), []).append(ds)
        if not tables:
            return {}
        params = {"pairs": list(tables)}  # bound server-side: names never enter SQL text
        pairs = "{pairs:Array(Tuple(String, String))}"
        rows = self.client.query(
            "SELECT database, name, engine, engine_full FROM system.tables "
            f"WHERE (database, name) IN {pairs}",
            parameters=params,
        ).result_rows
        cols = self.client.query(
            "SELECT database, table, groupArray(name) FROM system.columns "
            f"WHERE (database, table) IN {pairs} GROUP BY database, table",
            parameters=params,
        ).result_rows
        columns = {(db, t): frozenset(c) for db, t, c in cols}
        try:
            dicts = self.client.query(
                "SELECT database, name, attribute.names FROM system.dictionaries "
                f"WHERE (database, name) IN {pairs}",
                parameters=params,
            ).result_rows
        except Exception:  # needs an explicit grant; without it dictionaries are joined as tables
            dicts = []
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
        """Problems that only the database can reveal: missing sources and columns.

        Run as a user who can read everything; for a restricted user, missing
        means hidden, not broken."""
        problems = []
        for ds in self.full_model.datasets:
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
                if f"{ds.name}.{f.name}" in self.untranslatable_fields:
                    continue  # reported by untranslatable()
                for c in parse(pick_expression(f.expression)).find_all(exp.Column):
                    if not c.table:
                        needed[c.name] = f"field {f.name!r}"
            for col, where in needed.items():
                if col not in info.columns:
                    problems.append(
                        f"dataset {ds.name!r}: column {col!r} ({where}) not in {ds.source}"
                    )
        for r in self.full_model.relationships or []:
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
        rows = [tuple(_non_finite_to_none(v) for v in row) for row in res.result_rows]
        return Result(list(res.column_names), rows, sql)


def _non_finite_to_none(v):
    """ClickHouse yields nan and inf where SQL yields NULL; neither is JSON."""
    return None if isinstance(v, float) and not math.isfinite(v) else v
