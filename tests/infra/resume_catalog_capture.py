"""Regenerate the F6 fingerprint fixture from a bootstrapped, empty PostgreSQL DB.

Set DJANGO_SETTINGS_MODULE=config.settings.test and PYTHONPATH=.:tests. Point
MIGRATION_DATABASE_URL at separate disposable databases for the default and
``--guarded-first`` runs of ``python -m infra.resume_catalog_capture``. Then
run the same module with ``--merge normal.json guarded-first.json`` to emit the
fixture. ``--reapplied`` on a third empty DB emits the reversible-predecessor
companion. Production has no capture mode and never learns from a retry.
"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from unittest.mock import patch

from apps.tenancy.migrations import _resumable_schema
from apps.tenancy.migrations._resume_catalog import (
    ARTIFACT_STEP,
    Catalog,
    Delta,
    History,
    fingerprint,
    snapshot,
)
from django.db.migrations import SeparateDatabaseAndState
from django.db.migrations.executor import MigrationExecutor

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations import Migration
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ProjectState

type Forward = Callable[
    [str, "BaseDatabaseSchemaEditor", "ProjectState", "ProjectState"], None
]
type Fingerprints = dict[str, dict[str, str]]


class Capture(TypedDict):
    history: History
    fingerprints: Fingerprints


def merged(normal: Capture, guarded_first: Capture) -> Capture:
    """Combine writes observed in two clean orders, including redundant grants."""
    for name, steps in guarded_first["history"].items():
        by_step = dict(normal["history"][name])
        for step, writes in steps:
            for obj, patch_values in writes.items():
                target = by_step[step].setdefault(obj, {})
                for field, value in patch_values.items():
                    assert field not in target or target[field] == value
                    target[field] = value
    return normal


def _delta(before: Catalog, after: Catalog) -> Delta:
    result = {}
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name, {}), after.get(name, {})
        changes = {
            key: new.get(key)
            for key in sorted(old.keys() | new.keys())
            if old.get(key) != new.get(key)
        }
        if changes:
            result[name] = changes
    return result


def _step(migration: str, op: Operation, index: int) -> str | None:
    if isinstance(
        op, _resumable_schema.CheckpointedSchema | _resumable_schema.AtomicRunSQL
    ):
        return op.receipt
    if isinstance(op, _resumable_schema.ResumableRunSQL):
        return op.trigger
    if migration == "intake.0013_patient_demographics" and index:
        return f"demographics:{index}"
    return None


def _observe(
    forward: Forward, step: str, records: list[tuple[str | None, Catalog]]
) -> Forward:
    def wrapped(
        app: str,
        editor: BaseDatabaseSchemaEditor,
        before: ProjectState,
        after: ProjectState,
    ) -> None:
        forward(app, editor, before, after)
        records.append((step, snapshot(editor.connection)))

    return wrapped


def _instrument(
    stack: ExitStack,
    migration: Migration,
    name: str,
    records: list[tuple[str | None, Catalog]],
) -> None:
    if name == "ai.0003_artifact_retention":
        batch = migration.operations[0]
        assert isinstance(batch, SeparateDatabaseAndState)
        operations = batch.database_operations
        for index, op in enumerate(operations):
            key = f"legacy:{index}" if index < len(operations) - 1 else ARTIFACT_STEP
            stack.enter_context(
                patch.object(
                    op,
                    "database_forwards",
                    _observe(op.database_forwards, key, records),
                )
            )
        return
    for index, op in enumerate(migration.operations):
        step = _step(name, op, index)
        if step is not None:
            stack.enter_context(
                patch.object(
                    op,
                    "database_forwards",
                    _observe(op.database_forwards, step, records),
                )
            )


def _observe_plan(
    executor: MigrationExecutor, db: BaseDatabaseWrapper, *, guarded_first: bool
) -> tuple[History, Fingerprints, set[str]]:
    targets = executor.loader.graph.leaf_nodes()
    if guarded_first:
        # Some independent branches grant the same privilege. Observing the
        # guarded operations first captures their writes even when a later
        # full-graph install sees a redundant GRANT (no catalog delta).
        guarded = [
            key
            for key, m in sorted(executor.loader.disk_migrations.items())
            if key == ("ai", "0003_artifact_retention")
            or any(
                _step(".".join(key), op, i) is not None
                for i, op in enumerate(m.operations)
            )
        ]
        targets = guarded + targets
    graph = executor.loader.graph
    ordered = dict.fromkeys(
        node for target in targets for node in graph.forwards_plan(target)
    )
    executor.recorder.ensure_schema()
    previous = snapshot(db)
    history: History = {"bootstrap": [(None, _delta({}, previous))]}
    hashes: Fingerprints = {}
    scope: set[str] = set()
    state = executor.loader.project_state(nodes=[])
    for key in ordered:
        migration = executor.loader.get_migration(*key)
        name = f"{migration.app_label}.{migration.name}"
        records: list[tuple[str | None, Catalog]] = []
        with ExitStack() as stack:
            _instrument(stack, migration, name, records)
            state = executor.apply_migration(state, migration)
        records.append((None, snapshot(db)))
        changes: list[tuple[str | None, Delta]] = []
        migration_scope: set[str] = set()
        for step, observed in records:
            delta = _delta(previous, observed)
            previous = observed
            if step is not None or delta:
                changes.append((step, delta))
            if step is not None:
                migration_scope.update(delta)
        for step, observed in records:
            if step is not None:
                hashes[step] = {
                    object_name: fingerprint(observed.get(object_name, {}))
                    for object_name in sorted(migration_scope)
                }
        if changes:
            history[name] = changes
        scope.update(migration_scope)
    return history, hashes, scope


def capture(db: BaseDatabaseWrapper, *, guarded_first: bool = False) -> Capture:
    """Capture real DDL, with the guard detached only in this clean-run harness."""
    executor = MigrationExecutor(db)
    if executor.loader.applied_migrations:
        msg = "Fingerprint capture requires a clean, bootstrapped database"
        raise RuntimeError(msg)
    modules = (
        _resumable_schema,
        import_module("apps.ai.migrations.0003_artifact_retention"),
        import_module("apps.intake.migrations.0013_patient_demographics"),
    )
    with ExitStack() as stack:
        for module in modules:
            stack.enter_context(
                patch.multiple(
                    module,
                    checkpoint_applied=_unapplied,
                    require_checkpoint=_unchecked,
                )
            )
        history, hashes, scope = _observe_plan(
            executor, db, guarded_first=guarded_first
        )
    scoped: History = {}
    for migration_name, steps in history.items():
        deltas = [
            (step, {obj: patch for obj, patch in delta.items() if obj in scope})
            for step, delta in steps
        ]
        kept = [(step, delta) for step, delta in deltas if step is not None or delta]
        if kept:
            scoped[migration_name] = kept
    return {"history": scoped, "fingerprints": hashes}


def _unapplied(*_args: object) -> bool:
    return False


def capture_reapplied(db: BaseDatabaseWrapper) -> dict[str, Delta]:
    """Capture the exact extra physical slot left by identity's timezone round trip."""
    executor = MigrationExecutor(db)
    if executor.loader.applied_migrations:
        msg = "Reapply capture requires a clean, bootstrapped database"
        raise RuntimeError(msg)
    target = ("identity", "0005_clinic_timezone")
    parents = sorted(
        node.key for node in executor.loader.graph.node_map[target].parents
    )
    executor.migrate([target])
    normal = snapshot(db)
    MigrationExecutor(db).migrate(parents)
    MigrationExecutor(db).migrate([target])
    return {".".join(target): _delta(normal, snapshot(db))}


def _unchecked(*_args: object) -> None:
    return None


if __name__ == "__main__":
    import django
    from django.db import connection

    django.setup()
    result: Capture | dict[str, Delta]
    if sys.argv[1:2] == ["--reapplied"]:
        result = capture_reapplied(connection)
    elif sys.argv[1:2] == ["--merge"]:
        normal, alternate = (
            cast("Capture", json.loads(Path(arg).read_text(encoding="utf-8")))
            for arg in sys.argv[2:]
        )
        result = merged(normal, alternate)
    else:
        result = capture(connection, guarded_first="--guarded-first" in sys.argv[1:])
    sys.stdout.write(json.dumps(result, indent=1) + "\n")
