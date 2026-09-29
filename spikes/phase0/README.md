# Phase 0 spike: translation feasibility

Throwaway scripts. Not part of the library; kept for reproducibility of
[docs/phase-0-findings.md](../../docs/phase-0-findings.md).

## Reproduce

Requires a local ClickHouse on 127.0.0.1:8123 and a checkout of
https://github.com/apache/ossie next to this repository.

```bash
uv venv .venv && uv pip install --python .venv/bin/python sqlglot duckdb clickhouse-connect pyyaml ../ossie/python
```

Generate TPC-DS scale factor 1 and load the five tables the model uses:

```bash
.venv/bin/python -c "
import duckdb; c = duckdb.connect(); c.execute('INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf=1)')
for t in ['store_sales','date_dim','customer','item','store']:
    c.execute(f\"COPY {t} TO '{t}.parquet' (FORMAT PARQUET)\")"
clickhouse client -q "CREATE DATABASE IF NOT EXISTS tpcds"
for t in store_sales date_dim customer item store; do
  clickhouse client -q "CREATE TABLE tpcds.$t ENGINE=MergeTree ORDER BY tuple() AS SELECT * FROM file('$t.parquet', Parquet)"
done
```

`file()` reads from the server's `user_files_path`; copy the Parquet files
there first.

Run:

```bash
.venv/bin/python translate.py ../ossie/examples/tpcds_semantic_model.yaml
.venv/bin/python values.py
```

`results-translate.txt` and `results-values.txt` are the outputs from
2026-09-30 against ClickHouse 26.9.5, SQLGlot 30.20, Ossie main at adfa9e4.
