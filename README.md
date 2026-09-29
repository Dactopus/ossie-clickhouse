# ossie-clickhouse

An open-source implementation of the [Apache Ossie](https://github.com/apache/ossie) semantic model standard for ClickHouse.

## What it is

ossie-clickhouse makes data in ClickHouse understandable to AI agents and other tools. It takes a data description written in Apache Ossie, the open standard that records what tables and fields mean, how they relate, and how metrics are defined, and turns a question like "net revenue by channel for September" into a correct ClickHouse query. Whoever asks, the answer follows the same definitions.

## The problem it solves

A company connects an AI assistant to its database and the assistant gets things wrong: two hundred tables with no explanations, three columns with the same name, a different "revenue" in every department. The assistant guesses instead of knowing.

Apache Ossie solves this at the level of description: one definition of revenue for everyone. But until now there was no way to execute such descriptions on ClickHouse, and ClickHouse is not among the engines the standard lists. ossie-clickhouse closes that gap: the first implementation of the standard for ClickHouse, in the same way the community extension for DuckDB is for DuckDB.

## Who it is for

- **Companies with data in ClickHouse** that want an AI assistant to answer from their data rather than invent it. They describe the database once, in an open format, and any agent gets both the description and correct answers through this package.
- **Vendors building tools on top of ClickHouse**, including BI and AI assistants. Instead of each inventing its own way to understand customer data, they read a shared standard through a ready implementation.
- **Data engineers** who have already described their models in Ossie for Snowflake or dbt and want to bring them to ClickHouse without rewriting.

## What it does

1. Reads and validates a data description against the Ossie standard.
2. Translates metric definitions from the standard's portable expression language into ClickHouse SQL, and handles ClickHouse specifics on its own: which tables keep change history and need deduplication, which reference data lives in dictionaries. Users do not need to know about any of this.
3. Accepts a question in business terms: metrics, dimensions, filters. Works out which tables to join and how, and runs the query.
4. Serves the data description, including the standard's AI hints, to AI agents, and accepts their questions over MCP, the common protocol agents use to talk to external systems. Who may see what is set by a local access configuration.

## What it is not

It is not an analytics assistant: no chat, no charts, no user interface. It provides access and understanding; the interface is someone else's job. It is not a data warehouse or a loader: the data must already be in ClickHouse.

## Proposed technical approach

This is the intended design, not a description of working code.

- **Packaging.** A Python library with a command-line entry point. The MCP server is part of the same package behind an optional extra (`ossie-clickhouse[mcp]`), so the library can be imported without pulling in the MCP SDK.
- **Model loading.** Reads Ossie YAML or JSON and validates it against the official JSON schema from the Ossie repository. The supported schema version is pinned and checked explicitly, since the standard is still moving.
- **Expression translation.** Metric and field expressions are taken in `OSSIE_SQL_2026`, the standard's portable dialect, and translated to ClickHouse SQL with [SQLGlot](https://github.com/tobymao/sqlglot), which already has a ClickHouse dialect. An Ossie dialect for SQLGlot is being contributed upstream (apache/ossie PR #222); until it lands, `OSSIE_SQL_2026` is parsed as ANSI SQL, of which it is a subset. If a model carries no `OSSIE_SQL_2026` expression, the `ANSI_SQL` one is used.
- **Query planning.** A semantic query (metrics, dimensions, filters) is resolved against the model: datasets come from `source`, join paths from `relationships`, and the result is a single deterministic `SELECT`. Same model and same question always produce the same SQL.
- **ClickHouse specifics by introspection.** The executor asks ClickHouse rather than the user: `system.tables` tells it each table's engine (so it knows when a `ReplacingMergeTree` needs deduplication), and `system.dictionaries` tells it which reference data can be read with `dictGet`. Overrides live in the model's `custom_extensions` under `vendor_name: clickhouse`, a namespace owned by this project.
- **Execution.** Queries run through `clickhouse-connect` over HTTP, against self-hosted ClickHouse or ClickHouse Cloud alike.
- **Access control.** A local configuration maps roles to the datasets, fields, and metrics they may see. It is enforced inside the executor; the MCP layer only passes on who is asking.
- **MCP server.** A thin adapter over the library's public API exposing a handful of tools: list the model, describe an object with its `ai_context`, run a semantic query.
- **Testing.** Against the TPC-DS reference model shipped with the Ossie repository, loaded into a local ClickHouse, plus unit tests for translation.

## Terms

Open source under Apache 2.0. Installs with one command. Works with any ClickHouse, self-hosted or cloud, and with any description in the Ossie format. No runtime dependencies beyond ClickHouse itself and a few Python libraries.

## License

Apache License 2.0.
