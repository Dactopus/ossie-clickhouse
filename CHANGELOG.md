# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). While the major version is 0,
a minor release may change the Python API.

## [Unreleased]

### Fixed

- Functions the translator rewrites are checked against the spec's
  signature. Longer forms from other engines, such as Snowflake's
  `REGEXP_COUNT(str, pattern, position, flags)`, used to lose their extra
  arguments and return a different value; they are now rejected with the
  expected signature. Too few arguments give the same error instead of an
  `IndexError`, and `DATE_PART` accepts only the spec's date parts.
- One field or metric whose expression does not translate no longer stops
  the whole model. It is hidden like an object the user may not read,
  everything that depends on it goes with it, and `validate` names each
  one, with or without `--url`.

## [0.1.0] - 2026-09-30

First release. Ossie schema `0.2.0.dev0`, tested on ClickHouse 26.x with
the TPC-DS reference model.

- Model loading on `apache-ossie` with checks upstream lacks: unique names,
  relationships that resolve, expressions in a translatable dialect.
- Expression translation on the SQLGlot AST: the spec's function catalog
  mapped to ClickHouse, `OSSIE_SQL_2026` preferred, `ANSI_SQL` fallback,
  every catalog function checked by value against DuckDB or the spec text.
- Planner: one `SELECT` per question, `LEFT JOIN` along many-to-one
  relationships from one root dataset, `HAVING` for filters over metrics
  or aggregates, window metrics passed through, errors that name the
  nearest known object.
- Executor: `FINAL` for `Replacing*` engines, `dictGetOrNull` for
  single-key dictionaries, both learned from system tables; `inf` and
  `nan` returned as NULL; `{"dedup": "none"}` override in
  `custom_extensions`.
- Access control through ClickHouse: the model is trimmed to what the
  connected user may read; an optional policy file hides more per user or
  role.
- MCP server over stdio with `list_model`, `search_model`,
  `describe_object` and `query`.
- CLI: `validate`, `sql`, `query`, `serve`.
