"""Validate rehearsal declarations in the runner, outside the verifier process."""

from __future__ import annotations

import re
from inspect import getclosurevars
from types import FunctionType
from typing import TYPE_CHECKING

from django.db import connection, migrations
from django.db.migrations.loader import MigrationLoader

from ops.testing.migration_upgrade_rows import (
    declared_defaults,
    declared_transform_columns,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.db.migrations.operations.base import Operation


def _database_operations(operations: Iterable[Operation]) -> Iterator[Operation]:
    for operation in operations:
        if isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _database_operations(operation.database_operations)
        else:
            yield operation


def _nullable_additions(statement: str) -> set[tuple[str, str]]:
    additions = set()
    for table, clauses in re.findall(
        r"\bALTER TABLE\s+clinic_app\.(\w+)\s+([^;]+);", statement, re.IGNORECASE
    ):
        for clause in clauses.split(","):
            match = re.fullmatch(
                r"\s*ADD COLUMN (?:IF NOT EXISTS )?(\w+) "
                r"(?:uuid|bigint|text|bytea)(?: NULL)?\s*",
                clause,
                re.IGNORECASE,
            )
            if match:
                additions.add((table, match[1]))
    return additions


def _update_targets(statement: str) -> set[str]:
    # Only literal statements executed by the backfill qualify, not deferred
    # writes embedded in a CREATE FUNCTION body.
    statement = re.sub(r"(\$\w*\$).*?\1", "", statement, flags=re.DOTALL)
    return {
        f"{table}.{column}"
        for table, assignments in re.findall(
            r"\bUPDATE\s+clinic_app\.(\w+)(?:\s+AS\s+\w+)?\s+SET\s+"
            r"(.*?)(?=\bFROM\b|\bWHERE\b|;|$)",
            statement,
            re.IGNORECASE | re.DOTALL,
        )
        for column in re.findall(r"(?:^|,)\s*(\w+)\s*=", assignments)
    }


def _transform_statements(operation: Operation) -> Iterator[str]:
    if isinstance(operation, migrations.RunSQL) and isinstance(operation.sql, str):
        yield operation.sql
    if isinstance(operation, migrations.RunPython) and isinstance(
        operation.code, FunctionType
    ):
        code = operation.code
        values = (*getclosurevars(code).nonlocals.values(), *code.__code__.co_consts)
        for value in values:
            if isinstance(value, str):
                yield value


def validate_transform_declarations() -> None:
    """Bind declared transform targets to the actual migration data writes."""
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    for key, declared in sorted(declared_transform_columns().items()):
        migration = loader.disk_migrations.get(key)
        if (
            migration is None
            and key[0] == "comms"
            and ("comms", "0012_attempt_delivery") not in loader.disk_migrations
        ):
            continue
        if migration is None:
            message = f"unused fixture transform migration: {key}"
            raise RuntimeError(message)
        targets = {
            target
            for operation in _database_operations(migration.operations)
            for statement in _transform_statements(operation)
            for target in _update_targets(statement)
        }
        if targets != set(declared) or len(declared) != len(targets):
            message = f"fixture transform target mismatch: {key}: {sorted(targets)}"
            raise RuntimeError(message)


def _field_defaults(
    operations: Iterable[Operation], app: str
) -> dict[tuple[str, str], object]:
    editor = connection.schema_editor()
    added: dict[tuple[str, str], object] = {}
    for operation in _database_operations(operations):
        if isinstance(operation, migrations.AddField):
            field = operation.field.clone()
            field.set_attributes_from_name(operation.name)
            if field.column is None:
                continue
            actual = (
                field.db_default
                if field.has_db_default()
                else editor.effective_default(field)
            )
            if isinstance(actual, int) and not isinstance(actual, bool):
                actual = int(actual)
            added[(f"{app}_{operation.model_name.lower()}", field.column)] = actual
        elif isinstance(operation, migrations.RunSQL) and isinstance(
            operation.sql, str
        ):
            added.update(dict.fromkeys(_nullable_additions(operation.sql)))
    return added


def validate_default_declarations() -> None:
    """Require each declared default to match its AddField database operation."""
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    for (app, name), tables in sorted(declared_defaults().items()):
        if app not in loader.migrated_apps:
            continue
        migration = loader.disk_migrations.get((app, name))
        if migration is None:
            # The standalone candidate does not ship the optional inbox series.
            if app == "comms" and not any(
                owner == app and node.startswith("0012_")
                for owner, node in loader.disk_migrations
            ):
                continue
            message = f"unused fixture default migration: {app}.{name}"
            raise RuntimeError(message)
        added = _field_defaults(migration.operations, app)
        for table, columns in tables.items():
            for column, declared in columns.items():
                if (table, column) not in added:
                    message = f"unused fixture default declaration: {table}.{column}"
                    raise RuntimeError(message)
                actual = added[(table, column)]
                if type(actual) is not type(declared) or actual != declared:
                    message = f"wrong fixture SQL default declaration: {table}.{column}"
                    raise RuntimeError(message)
