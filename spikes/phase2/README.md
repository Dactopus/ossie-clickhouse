# Phase 2 spike: apache/ossie PR #222 evaluation

Runs the Phase 0 corpus (spec function catalog plus the TPC-DS model) through
the `ossie_sql` SQLGlot dialect from PR #222 and compares with SQLGlot's
default dialect. Findings in
[docs/phase-2-pr222-evaluation.md](../../docs/phase-2-pr222-evaluation.md).

Setup as in [spikes/phase0](../phase0/README.md), plus the PR branch:

```bash
git -C ../ossie fetch --depth 1 origin pull/222/head:pr222
git -C ../ossie worktree add ../ossie-pr222 pr222
uv pip install --python .venv/bin/python ../ossie-pr222/core/python
.venv/bin/python pr222.py
```

`results-pr222.txt` is the output from 2026-09-30 against PR head 5ddda46,
SQLGlot 30.20, ClickHouse 26.9.5.
