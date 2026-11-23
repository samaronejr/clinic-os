"""Check whole PostgreSQL objects against clean migration boundaries."""

from __future__ import annotations

import json
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from django.db.migrations.recorder import MigrationRecorder

from apps.tenancy.migrations._resume_catalog_sql import catalog_sql, fields

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

type Definition = dict[str, str]
type Catalog = dict[str, Definition]
type Delta = dict[str, dict[str, str | None]]
type History = dict[str, list[tuple[str | None, Delta]]]

FIXTURE = Path(__file__).with_name("_resume_catalog_pg16.json")
ARTIFACT_STEP = "artifact-retention:0"


class ResumeCatalogError(RuntimeError):
    """The schema does not match any committed prefix of the migration."""


class Checkpoint(NamedTuple):
    """A migration's capture order, its owned scope and its requested prefix."""

    migration: str
    scope: frozenset[str]
    prefix: int


def snapshot(connection: BaseDatabaseWrapper) -> Catalog:
    """Collect complete object definitions from a single catalog statement."""
    query = catalog_sql(fields(connection))
    with connection.cursor() as cursor:
        cursor.execute(query)
        catalog: Catalog = {}
        for name, path, payload in cursor.fetchall():
            definition = catalog.setdefault(name, {})
            for attribute, value in json.loads(payload).items():
                key = ".".join([*path, attribute])
                if key in definition:
                    msg = f"Ambiguous catalog attribute for {name}.{key}"
                    raise ResumeCatalogError(msg)
                definition[key] = json.dumps(value, sort_keys=True)
                if attribute in {"relacl", "attacl", "proacl"} and isinstance(
                    value, dict
                ):
                    # Independent grants can be installed by independent
                    # branches. Keep each privilege as its own replay fact.
                    definition[key] = "{}"
                    definition.update(
                        (f"{key}.{grant}", json.dumps(option))
                        for grant, option in value.items()
                    )
    return catalog


def _overlay(catalog: Catalog, delta: Delta) -> Catalog:
    result = catalog.copy()
    for name, patch in delta.items():
        if name not in result:
            continue
        definition = result[name].copy()
        for key, value in patch.items():
            if value is None:
                definition.pop(key, None)
            else:
                definition[key] = value
        result[name] = definition
    return result


@cache
def history() -> History:
    """Read the captured clean object history in migration execution order."""
    return cast("History", json.loads(FIXTURE.read_text(encoding="utf-8"))["history"])


@cache
def _checkpoints() -> dict[str, Checkpoint]:
    checkpoints = {}
    for migration, steps in history().items():
        guarded = [delta for step, delta in steps if step is not None]
        scope = frozenset().union(*(delta.keys() for delta in guarded))
        for prefix, (step, _) in enumerate(steps, 1):
            # The first three actual DDL statements of the older AI migration
            # could commit separately. Only the fourth can be recorded.
            if step is not None and not step.startswith("legacy:"):
                checkpoints[step] = Checkpoint(migration, scope, prefix)
    return checkpoints


def _boundaries(
    checkpoint: Checkpoint, applied: set[str], object_names: frozenset[str]
) -> list[Catalog]:
    catalog: Catalog = {name: {} for name in object_names}
    for migration, operations in history().items():
        if migration == "bootstrap" or (
            migration != checkpoint.migration and migration in applied
        ):
            for _, delta in operations:
                catalog = _overlay(catalog, delta)
    states = [catalog]
    for _, delta in history()[checkpoint.migration]:
        states.append(_overlay(states[-1], delta))
    return states


def fingerprint(definition: Mapping[str, str]) -> str:
    """Hash the complete canonical definition, including member names."""
    return sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


_MEMBER_GROUPS = frozenset(
    {"columns", "constraints", "indexes", "policies", "triggers", "constraint_triggers"}
)


def _first_difference(actual: Definition, expected: Definition) -> str:
    for key in sorted(
        actual.keys() | expected.keys(),
        key=lambda k: (
            k.partition(".")[0] not in _MEMBER_GROUPS,
            k not in actual or k not in expected,
            k,
        ),
    ):
        if actual.get(key) != expected.get(key):
            parts = key.split(".")
            if parts[0] in _MEMBER_GROUPS:
                parts = parts[1:]
            parts = [
                {"attstorage": "storage", "relacl": "grants", "proacl": "grants"}.get(
                    part, part
                )
                for part in parts
            ]
            change = (
                "unexpected"
                if key not in expected
                else "missing"
                if key not in actual
                else "differs"
            )
            return f"{'.'.join(parts)} ({change})"
    msg = "Unequal catalog fingerprints without a differing attribute"
    raise ResumeCatalogError(msg)


def checkpoint_applied(editor: BaseDatabaseSchemaEditor, step: str) -> bool:
    """Accept only an exact whole-object catalog at a clean captured boundary."""
    if editor.collect_sql:
        return False
    checkpoint = _checkpoints()[step]
    recorder = MigrationRecorder(editor.connection)
    applied = {f"{app}.{name}" for app, name in recorder.applied_migrations()}
    live = snapshot(editor.connection)
    actual = {name: live.get(name, {}) for name in checkpoint.scope}
    return match_boundary(
        checkpoint, actual, _boundaries(checkpoint, applied, checkpoint.scope)
    )


def match_boundary(
    checkpoint: Checkpoint, actual: Catalog, states: list[Catalog]
) -> bool:
    """Use this same decision for DB reads and PostgreSQL catalog-row witnesses."""
    hashes = {name: fingerprint(definition) for name, definition in actual.items()}
    closest: tuple[int, int, list[str]] | None = None
    for index in range(len(states) - 1, -1, -1):
        expected = states[index]
        names = [
            name
            for name in sorted(expected)
            if fingerprint(expected[name]) != hashes[name]
        ]
        if not names:
            return index >= checkpoint.prefix
        # Choose the closest boundary just to diagnose. No partial match is
        # accepted: every complete object fingerprint must match one boundary.
        fields = sum(
            actual[name].get(key) != expected[name].get(key)
            for name in names
            for key in actual[name].keys() | expected[name].keys()
        )
        if closest is None or fields < closest[0]:
            closest = fields, index, names
    if closest is None:
        msg = f"{checkpoint.migration} has no captured catalog boundaries"
        raise ResumeCatalogError(msg)
    _, index, names = closest
    details = ", ".join(
        f"{name}.{_first_difference(actual[name], states[index][name])}"
        for name in names
    )
    msg = f"{checkpoint.migration} cannot resume: conflicting catalog for {details}"
    raise ResumeCatalogError(msg)


def require_checkpoint(editor: BaseDatabaseSchemaEditor, step: str) -> None:
    """Refuse to record a unit unless it published its expected definitions."""
    if not editor.collect_sql and not checkpoint_applied(editor, step):
        msg = f"Protected schema unit did not produce the catalog for {step}"
        raise ResumeCatalogError(msg)
