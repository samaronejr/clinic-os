"""Check whole PostgreSQL objects against clean migration boundaries."""

from __future__ import annotations

import json
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from django.db.migrations.recorder import MigrationRecorder

from apps.tenancy.migrations import _default_witness
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
ResumeCatalogError = _default_witness.ResumeCatalogError


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
) -> list[tuple[int, Catalog]]:
    catalog: Catalog = {name: {} for name in object_names}
    for migration, operations in history().items():
        if migration == "bootstrap" or (
            migration != checkpoint.migration and migration in applied
        ):
            for _, delta in operations:
                catalog = _overlay(catalog, delta)
    states = [(0, catalog)]
    for prefix, (_, delta) in enumerate(history()[checkpoint.migration], 1):
        catalog = _overlay(catalog, delta)
        states.append((prefix, catalog))
    return states


def fingerprint(definition: Mapping[str, str]) -> str:
    """Hash the complete canonical definition, including member names."""
    return sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


_MEMBER_GROUPS = frozenset(
    {"columns", "constraints", "indexes", "policies", "triggers", "children", "parents"}
)


def _differences(
    actual: Definition, expected: Definition, materialized: dict[str, str]
) -> set[str]:
    if actual == expected:
        return set()
    conflicts = {
        key
        for key in actual.keys() | expected.keys()
        if actual.get(key) != expected.get(key)
    }
    # A heap rewrite stores old fast defaults in the tuples. Compare the two
    # fields as a pair, in this direction only, for this reference boundary.
    for flag in tuple(conflicts):
        if flag.startswith("columns.") and flag.endswith(".atthasmissing"):
            value = flag.removesuffix("atthasmissing") + "attmissingval"
            if (
                expected.get(flag) == "true"
                and expected.get(value) not in {None, "null"}
                and actual.get(flag) == "false"
                and actual.get(value) == "null"
            ):
                column = flag.removeprefix("columns.").removesuffix(".atthasmissing")
                materialized[column] = expected[value]
                conflicts.difference_update((flag, value))
    return conflicts


def _first_difference(keys: set[str], actual: Definition, expected: Definition) -> str:
    key = min(
        keys,
        key=lambda k: (
            k.partition(".")[0] not in _MEMBER_GROUPS,
            k not in actual or k not in expected,
            not k.endswith(".atttypid"),
            k,
        ),
    )
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


def checkpoint_applied(editor: BaseDatabaseSchemaEditor, step: str) -> bool:
    """Accept only an exact whole-object catalog at a clean captured boundary."""
    if editor.collect_sql:
        return False
    db = editor.connection
    checkpoint = _checkpoints()[step]
    _default_witness.lock_scope(db, checkpoint.scope)
    recorder = MigrationRecorder(db)
    applied = {f"{app}.{name}" for app, name in recorder.applied_migrations()}
    live = snapshot(db)
    actual = {name: live.get(name, {}) for name in checkpoint.scope}
    already = match_boundary(
        checkpoint, actual, _boundaries(checkpoint, applied, checkpoint.scope), db=db
    )
    if not already:
        # No mismatched predecessor, including an already cleared, unwitnessed
        # default, may produce its own proof. These inserts commit with the DDL.
        _default_witness.capture(db, checkpoint.migration, checkpoint.scope)
    return already


def match_boundary(
    checkpoint: Checkpoint,
    actual: Catalog,
    states: list[tuple[int, Catalog]],
    *,
    db: BaseDatabaseWrapper | None = None,
) -> bool:
    """Use this same decision for DB reads and PostgreSQL catalog-row witnesses."""
    closest: tuple[int, Catalog, dict[str, set[str]]] | None = None
    for prefix, expected in reversed(states):
        conflicts = {}
        proofs: dict[str, dict[str, str]] = {}
        for name in sorted(expected):
            columns: dict[str, str] = {}
            if keys := _differences(actual[name], expected[name], columns):
                conflicts[name] = keys
            if columns:
                proofs[name] = columns
        if not conflicts:
            _prove_materializations(db, checkpoint.migration, proofs)
            return prefix >= checkpoint.prefix
        # Distance selects the diagnostic only; never combine different prefixes.
        fields = sum(map(len, conflicts.values()))
        if closest is None or fields < closest[0]:
            closest = fields, expected, conflicts
    if closest is None:
        msg = f"{checkpoint.migration} has no captured catalog boundaries"
        raise ResumeCatalogError(msg)
    _, expected, conflicts = closest
    details = ", ".join(
        f"{name}.{_first_difference(keys, actual[name], expected[name])}"
        for name, keys in conflicts.items()
    )
    msg = f"{checkpoint.migration} cannot resume: conflicting catalog for {details}"
    raise ResumeCatalogError(msg)


def _prove_materializations(
    db: BaseDatabaseWrapper | None, migration: str, proofs: dict[str, dict[str, str]]
) -> None:
    refused = []
    for name, columns in proofs.items():
        table = name.removeprefix("relation ")
        for column, value in sorted(columns.items()):
            try:
                if db is None:
                    raise _default_witness.missing(table, column)
                _default_witness.verify(db, migration, table, column, value)
            except ResumeCatalogError as exc:
                refused.append(str(exc))
    if refused:
        raise ResumeCatalogError("; ".join(refused))


def require_checkpoint(editor: BaseDatabaseSchemaEditor, step: str) -> None:
    """Refuse to record a unit unless it published its expected definitions."""
    if editor.collect_sql:
        return
    if not checkpoint_applied(editor, step):
        msg = f"Protected schema unit did not produce the catalog for {step}"
        raise ResumeCatalogError(msg)
    # Include defaults added by this batch, before its first transaction ends.
    checkpoint = _checkpoints()[step]
    _default_witness.capture(editor.connection, checkpoint.migration, checkpoint.scope)
