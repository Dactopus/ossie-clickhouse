# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

ossie-clickhouse is an open-source implementation of the
[Apache Ossie](https://github.com/apache/ossie) semantic model standard for
ClickHouse. It reads an Ossie data description, translates metric definitions
into ClickHouse SQL, answers questions posed in business terms (metrics,
dimensions, filters), and serves the description and queries to AI agents over
MCP. See [README.md](README.md) for the full product description.

Licensed under Apache 2.0.

## Language

All text in this repository is in English only: code identifiers, comments,
docstrings, commit messages, documentation, issues, and test names. Do not
introduce text in any other language, even in examples or fixtures.

## Scope

In scope:

- Reading and validating Ossie descriptions.
- Translating Ossie expressions into ClickHouse SQL, including ClickHouse
  specifics the user must not have to think about: deduplication of tables that
  keep change history, reference data stored in dictionaries, join resolution.
- Executing queries against any ClickHouse (self-hosted or cloud).
- Serving the model and accepting questions over MCP, with access controlled
  by a local configuration.

Out of scope, do not build:

- Chat, charts, or any user interface.
- Data loading, ETL, or warehouse features. Data is already in ClickHouse.
- Dependencies beyond ClickHouse and the Ossie standard where avoidable.

## Design principles

- The Ossie description is the single source of truth. Never hardcode metric
  or table semantics that belong in the model.
- Follow the standard's semantics exactly. When ClickHouse behaviour differs
  from the standard's assumptions, handle it in the translator, not by
  changing what the standard means.
- Every answer must be reproducible: the same question and the same model
  yield the same SQL.
- Keep the public surface small. Prefer one obvious way to do each thing.

## Stack

Python library with a CLI entry point. Details in the README section
"Proposed technical approach"; the parts that constrain code:

- **SQLGlot** for expression translation. Parse `OSSIE_SQL_2026` as ANSI SQL
  until the upstream Ossie dialect (apache/ossie PR #222) lands, then switch.
  Fall back to a model's `ANSI_SQL` expression when `OSSIE_SQL_2026` is absent.
- **clickhouse-connect** over HTTP for execution.
- **MCP SDK** only behind the `[mcp]` extra. The core library must import
  without it.
- **Ossie JSON schema** from the Ossie repository, with the supported schema
  version pinned and checked explicitly.

## Architecture rules

- Layering: model loader -> translator -> planner -> executor -> MCP adapter.
  Lower layers never import higher ones.
- The MCP server is a thin adapter over the public library API. No logic
  lives only in the MCP layer.
- Access control is enforced in the executor. The MCP layer only passes on
  the caller's identity.
- ClickHouse specifics (engine-based deduplication, `dictGet` for
  dictionaries) come from introspecting `system.tables` and
  `system.dictionaries`, not from asking the user. Overrides go in the model's
  `custom_extensions` under `vendor_name: clickhouse`.
- Query planning is deterministic: datasets from `source`, joins from
  `relationships`, one `SELECT` per question.

## Testing

Unit tests for translation. Integration tests against the TPC-DS reference
model from the Ossie repository, loaded into a local ClickHouse.

## Repository status

Greenfield. No code or build tooling exists yet. When they do, add the
install, test, and lint commands to this file.
