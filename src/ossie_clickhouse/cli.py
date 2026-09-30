"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from ossie_clickhouse.access import Policy
from ossie_clickhouse.executor import Executor, connect
from ossie_clickhouse.model import ModelError, load_model
from ossie_clickhouse.planner import PlanError, Planner, Query


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossie-clickhouse")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check an Ossie model file")
    v.add_argument("model", help="path to a YAML or JSON Ossie model")
    v.add_argument("--url", help="also check the model against this ClickHouse")
    for name, help_ in (("sql", "build the ClickHouse SQL for a semantic query"),
                        ("query", "run a semantic query and print the rows")):  # fmt: skip
        s = sub.add_parser(name, help=help_)
        s.add_argument("model", help="path to a YAML or JSON Ossie model")
        s.add_argument("-m", "--metric", action="append", default=[], help="metric name")
        s.add_argument("-d", "--dimension", action="append", default=[], help="dataset.field")
        s.add_argument(
            "-f",
            "--filter",
            action="append",
            default=[],
            help="condition over dataset.field or a metric name",
        )
        s.add_argument("-o", "--order", action="append", default=[], help="name or 'name desc'")
        s.add_argument("-l", "--limit", type=int)
        s.add_argument("--url", help="ClickHouse HTTP URL (default $OSSIE_CLICKHOUSE_URL)")
        s.add_argument("--policy", help="YAML file hiding objects per ClickHouse user or role")
    sub.choices["query"].add_argument(
        "--json", action="store_true", help="JSON rows instead of TSV"
    )
    sv = sub.add_parser("serve", help="run the MCP server over stdio (needs the [mcp] extra)")
    sv.add_argument("model", help="path to a YAML or JSON Ossie model")
    sv.add_argument("--url", help="ClickHouse HTTP URL (default $OSSIE_CLICKHOUSE_URL)")
    sv.add_argument("--policy", help="YAML file hiding objects per ClickHouse user or role")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from ossie_clickhouse.mcp_server import build_server

        try:
            policy = Policy.load(args.policy) if args.policy else None
            server = build_server(load_model(args.model), lambda: connect(args.url), policy)
        except ModelError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        server.run()  # stdio
        return 0

    if args.command in ("sql", "query"):
        try:
            model = load_model(args.model)
            q = Query(
                tuple(args.metric),
                tuple(args.dimension),
                tuple(args.filter),
                tuple(args.order),
                args.limit,
            )
            if args.command == "sql" and not args.url:
                print(Planner(model).sql(q, pretty=True))
                return 0
            policy = Policy.load(args.policy) if args.policy else None
            ex = Executor(connect(args.url), model, policy)
            if args.command == "sql":
                print(ex.planner.sql(q, pretty=True))
                return 0
            r = ex.execute(q)
        except (ModelError, PlanError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.json:
            rows = [dict(zip(r.columns, row, strict=True)) for row in r.rows]
            print(
                json.dumps(rows, default=lambda v: float(v) if isinstance(v, Decimal) else str(v))
            )
        else:
            print("\t".join(r.columns))
            for row in r.rows:
                print("\t".join("" if v is None else str(v) for v in row))
        return 0

    if args.command == "validate":
        try:
            doc = load_model(args.model)
        except ModelError as e:
            for p in e.problems:
                print(f"error: {p}", file=sys.stderr)
            print(f"{args.model}: {len(e.problems)} problem(s)", file=sys.stderr)
            return 1
        if args.url:
            problems = Executor(connect(args.url), doc).check()
            for p in problems:
                print(f"error: {p}", file=sys.stderr)
            if problems:
                print(
                    f"{args.model}: {len(problems)} problem(s) against {args.url}", file=sys.stderr
                )
                return 1
        n_fields = sum(len(d.fields or []) for d in doc.datasets)
        print(
            f"{args.model}: ok ({doc.name}, version {doc.version}, "
            f"{len(doc.datasets)} datasets, {n_fields} fields, "
            f"{len(doc.relationships or [])} relationships, {len(doc.metrics or [])} metrics)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
