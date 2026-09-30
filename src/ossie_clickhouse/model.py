"""Load an Ossie model from a file and check what the upstream package does not.

The ``apache-ossie`` package validates structure only. Everything the later
layers rely on (a pinned spec version, names that resolve, expressions in a
dialect we can translate) is checked here, and all problems are reported at
once rather than one per run.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from ossie import OssieDialect, OssieDocument
from pydantic import ValidationError

SUPPORTED_VERSION = "0.2.0.dev0"
SUPPORTED_DIALECTS = frozenset({OssieDialect.OSSIE_SQL_2026, OssieDialect.ANSI_SQL})


class ModelError(ValueError):
    """A model that cannot be used. ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def load_model(path: str | Path) -> OssieDocument:
    """Read a YAML or JSON Ossie model and validate it. Raises ``ModelError``."""
    path = Path(path)
    try:
        text = path.read_text()
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise ModelError([f"cannot read {path}: {e}"]) from e
    if not isinstance(data, dict):
        raise ModelError([f"{path}: top level must be a mapping"])
    try:
        doc = OssieDocument.model_validate(data)
    except ValidationError as e:
        raise ModelError([_format(err) for err in e.errors()]) from e
    problems = check_model(doc)
    if problems:
        raise ModelError(problems)
    return doc


def check_model(doc: OssieDocument) -> list[str]:
    """Semantic checks beyond the schema. Returns human-readable problems."""
    problems: list[str] = []
    if doc.version != SUPPORTED_VERSION:
        problems.append(
            f"unsupported version {doc.version!r}; this build supports {SUPPORTED_VERSION!r}"
        )

    datasets = {d.name: d for d in doc.datasets}
    problems += _duplicates("dataset", [d.name for d in doc.datasets])
    problems += _duplicates("metric", [m.name for m in doc.metrics or []])
    problems += _duplicates("relationship", [r.name for r in doc.relationships or []])

    # Keys and relationship columns are physical column names per the spec, not
    # field names, so their existence can only be checked against the database.
    for d in doc.datasets:
        problems += _duplicates(f"field in dataset {d.name!r}", [f.name for f in d.fields or []])
        for f in d.fields or []:
            problems += _dialect_check(f"field {d.name}.{f.name}", f.expression.dialects)

    for m in doc.metrics or []:
        problems += _dialect_check(f"metric {m.name!r}", m.expression.dialects)

    for r in doc.relationships or []:
        if len(r.from_columns) != len(r.to_columns):
            problems.append(
                f"relationship {r.name!r}: from_columns and to_columns differ in length"
            )
        for side, ds_name in (("from", r.from_dataset), ("to", r.to)):
            if ds_name not in datasets:
                problems.append(
                    f"relationship {r.name!r}: {side} dataset {ds_name!r} does not exist"
                )
    return problems


def _duplicates(kind: str, names: list[str]) -> list[str]:
    seen: set[str] = set()
    return [f"duplicate {kind} name {n!r}" for n in names if n in seen or seen.add(n)]


def _dialect_check(what: str, dialects) -> list[str]:
    if any(d.dialect in SUPPORTED_DIALECTS for d in dialects):
        return []
    have = ", ".join(sorted(d.dialect.value for d in dialects))
    return [f"{what}: no expression in a supported dialect (has {have}; need ANSI_SQL)"]


def _format(err: dict) -> str:
    loc = ".".join(str(p) for p in err["loc"]) or "document"
    return f"{loc}: {err['msg']}"
