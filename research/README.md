# Research

Experiments behind some decisions in this code, and data we share with the
Apache Ossie community. Each study has its own folder, named by the date of
its run, with the corpus, the script, its raw output and a README saying how
to reproduce it. Nothing here is part of the library: the package, the CLI
and the tests never import from this folder, and a study pins its own
dependencies.

- [2026-10-02-pr222-sqlglot-dialect](2026-10-02-pr222-sqlglot-dialect/):
  the Ossie SQLGlot dialect from apache/ossie PR #222 against ClickHouse,
  next to SQLGlot's default and Snowflake parsers, and how many ClickHouse
  divergences come from SQLGlot's generator.
- [2026-10-10-execute-query-profile](2026-10-10-execute-query-profile/):
  the MCP tool against the `execute_query` profile draft of
  apache/ossie#529 and its offline checker, and Layer 3 refusal codes and
  answers on a model with two unrelated facts.
