"""Validate F6 checkpoints against the frozen, clean PostgreSQL 16 catalog."""

from __future__ import annotations

import json
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

from apps.tenancy.migrations._resume_catalog_sql import CATALOG_SQL

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

type Definition = str | None
type Catalog = dict[str, Definition]
type Changes = dict[str, tuple[Definition, Definition]]
type Checkpoint = tuple[str, tuple[Catalog, ...], int]

_AI = "artifact-retention:0"
_CONTENT = "column ai_aiartifact.content"
_PURGED = "column ai_aiartifact.purged_at"
_CHECK = "constraint ai_aiartifact.ai_artifact_values"


class ResumeCatalogError(RuntimeError):
    """A migration cannot tell a conflicting catalog from a partial publication."""


def _boundaries(steps: list[Changes], *, artifact: bool) -> tuple[Catalog, ...]:
    initial: Catalog = {}
    for changes in steps:
        for name, (before, _) in changes.items():
            initial.setdefault(name, before)
    states = [initial]
    for changes in steps:
        after = states[-1] | {name: end for name, (_, end) in changes.items()}
        if artifact:
            # The pre-F6 migration could commit these three operations alone.
            # Its exact legacy prefixes are safe to finish, never to record.
            nullable = initial | {_CONTENT: after[_CONTENT]}
            added = nullable | {_PURGED: after[_PURGED]}
            states.extend((nullable, added, added | {_CHECK: None}))
        states.append(after)
    return tuple(states)


@cache
def _checkpoints() -> dict[str, Checkpoint]:
    fixture = Path(__file__).with_name("_resume_catalog_pg16.json")
    migrations = cast(
        "dict[str, dict[str, Changes]]", json.loads(fixture.read_text(encoding="utf-8"))
    )
    checkpoints: dict[str, Checkpoint] = {}
    for migration, steps in migrations.items():
        artifact = _AI in steps
        states = _boundaries(list(steps.values()), artifact=artifact)
        for index, step in enumerate(steps, start=4 if artifact else 1):
            checkpoints[step] = migration, states, index
    return checkpoints


def snapshot(editor: BaseDatabaseSchemaEditor) -> Catalog:
    """Read deparsed definitions, independent of physical database OIDs."""
    with editor.connection.cursor() as cursor:
        cursor.execute(CATALOG_SQL)
        return {
            name: sha256(definition.encode("utf-8")).hexdigest()
            for name, definition in cursor.fetchall()
        }


def checkpoint_applied(editor: BaseDatabaseSchemaEditor, step: str) -> bool:
    """Match a whole committed prefix, including objects modified by later steps."""
    if editor.collect_sql:
        return False
    migration, states, index = _checkpoints()[step]
    catalog = snapshot(editor)
    mismatches = [
        [name for name, expected in state.items() if catalog.get(name) != expected]
        for state in states
    ]
    for boundary in range(len(states) - 1, -1, -1):
        if not mismatches[boundary]:
            return boundary >= index
    # Pick the closest known boundary for actionable diagnostics. Ties prefer
    # the later boundary (e.g. a same-name index with a conflicting definition).
    closest = min(reversed(mismatches), key=len)
    msg = f"{migration} cannot resume: conflicting catalog for {', '.join(closest)}"
    raise ResumeCatalogError(msg)


def require_checkpoint(editor: BaseDatabaseSchemaEditor, step: str) -> None:
    """Refuse to record a unit unless it published its expected definitions."""
    if not editor.collect_sql and not checkpoint_applied(editor, step):
        msg = f"Protected schema unit did not produce the catalog for {step}"
        raise ResumeCatalogError(msg)
