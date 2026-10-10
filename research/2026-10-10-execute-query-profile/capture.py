"""Capture execute_query exchanges for the profile's offline checker.

Runs the MCP server of this repository in process, lists its execute_query
tool and calls it once per case, writing what came over the protocol in the
capture format of apache/ossie#529 (validation/mcp/capture.schema.json).

A case is {"name", "query"}, sent with the server's own data_source_id, or
{"name", "arguments"}, sent as given; "$SOURCE" in the arguments stands for
the server's data_source_id.

Usage: python capture.py MODEL.yaml CASES.json OUT.json [--url URL] [--setup SQL]
  --url    ClickHouse, default $OSSIE_CLICKHOUSE_URL or http://127.0.0.1:8123
  --setup  statements (separated by ";" at a line end) to run first
"""

import argparse
import asyncio
import json
import os

from mcp import Client

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.execute_query import Binding
from dactopus_ossie_clickhouse.executor import connect
from dactopus_ossie_clickhouse.mcp_server import build_server


def wire(model) -> dict:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


async def capture(model_path: str, cases_path: str, url: str) -> dict:
    model = load_model(model_path)
    source = Binding.of(model).data_source_id
    with open(cases_path) as f:
        cases = json.load(f)
    out = []
    # The client checks every structuredContent against the tool's outputSchema.
    async with Client(build_server(model, lambda: connect(url))) as c:
        tools = await c.list_tools()
        tool = next(t for t in tools.tools if t.name == "execute_query")
        for case in cases:
            if "arguments" in case:
                args = json.loads(json.dumps(case["arguments"]).replace("$SOURCE", source))
            else:
                args = {"data_source_id": source, "query": case["query"]}
            result = await c.call_tool("execute_query", args)
            out.append({"name": case["name"], "arguments": args, "result": wire(result)})
    return {"protocol_version": "2026-07-28", "tool": wire(tool), "cases": out}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("model")
    p.add_argument("cases")
    p.add_argument("out")
    p.add_argument("--url", default=os.environ.get("OSSIE_CLICKHOUSE_URL", "http://127.0.0.1:8123"))
    p.add_argument("--setup")
    a = p.parse_args()
    if a.setup:
        client = connect(a.url)
        with open(a.setup) as f:
            for statement in f.read().split(";\n"):
                if statement.strip():
                    client.command(statement)
    result = asyncio.run(capture(a.model, a.cases, a.url))
    with open(a.out, "w") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()
