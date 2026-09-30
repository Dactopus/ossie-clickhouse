"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from ossie_clickhouse.model import ModelError, load_model
from ossie_clickhouse.planner import PlanError, Planner, Query


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossie-clickhouse")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check an Ossie model file")
    v.add_argument("model", help="path to a YAML or JSON Ossie model")
    s = sub.add_parser("sql", help="build the ClickHouse SQL for a semantic query")
    s.add_argument("model", help="path to a YAML or JSON Ossie model")
    s.add_argument("-m", "--metric", action="append", default=[], help="metric name")
    s.add_argument("-d", "--dimension", action="append", default=[], help="dataset.field")
    s.add_argument("-f", "--filter", action="append", default=[], help="Ossie expression")
    s.add_argument("-l", "--limit", type=int)
    args = parser.parse_args(argv)

    if args.command == "sql":
        try:
            planner = Planner(load_model(args.model))
            q = Query(tuple(args.metric), tuple(args.dimension), tuple(args.filter), args.limit)
            print(planner.sql(q, pretty=True))
        except (ModelError, PlanError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.command == "validate":
        try:
            doc = load_model(args.model)
        except ModelError as e:
            for p in e.problems:
                print(f"error: {p}", file=sys.stderr)
            print(f"{args.model}: {len(e.problems)} problem(s)", file=sys.stderr)
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
