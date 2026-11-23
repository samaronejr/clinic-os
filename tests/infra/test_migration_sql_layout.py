"""Pin Wave migration SQL bytes and catch new inline policy/guard literals."""

from __future__ import annotations

import ast
import json
import re
from hashlib import sha256
from importlib import import_module
from inspect import getclosurevars
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from django.apps import apps
from django.db import migrations
from django.db.migrations.loader import MigrationLoader

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.db.migrations.operations.base import Operation

BASE = "4f8d2454987ca1d083ead53a7501126d647a89bc"
FIXTURE = Path(__file__).with_name("migration_sql_at_4f8d2454.json")
POLICY_DDL = re.compile(
    r"\b(?:(?:CREATE(?:\s+OR\s+REPLACE)?|ALTER|DROP|ENABLE|DISABLE)\s+"
    r"(?:(?:CONSTRAINT\s+)?TRIGGER|POLICY|FUNCTION|PROCEDURE)\b"
    r"|(?:ENABLE|DISABLE|FORCE)\s+ROW\s+LEVEL\s+SECURITY\b"
    r"|(?:GRANT|REVOKE)\s+[\w\s,()]*?\bON\b"
    r"|(?:ADD|DROP)\s+CONSTRAINT\b|\bRAISE\s+EXCEPTION\b)",
    re.IGNORECASE,
)


def first_party_migrations() -> dict[str, migrations.Migration]:
    return {
        f"{app}.{name}": migration
        for (app, name), migration in MigrationLoader(None).disk_migrations.items()
        if apps.get_app_config(app).name.startswith("apps.")
    }


def _operations(operations: Iterable[Operation]) -> Iterator[Operation]:
    for operation in operations:
        if isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _operations(operation.database_operations)
        else:
            yield operation


def _digest(value: object) -> str:
    # An SQL string hashes its actual UTF-8 bytes, including leading whitespace.
    # Positional SQL/parameters are serialized with their exact strings intact.
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def _python_sql(operation: migrations.RunPython) -> object:
    """Capture the SQL-bearing callbacks affected by the layout refactor."""
    code = operation.code
    if getattr(code, "__qualname__", "") == "_sql_step.<locals>.run":
        assert operation.atomic
        assert operation.reverse_code is None
        # The new read-only catalog probes are not part of the frozen script.
        return getclosurevars(code).nonlocals["sql"]
    if getattr(code, "__name__", "") not in {
        "backfill",
        "finish_foreign_keys",
        "install_alias_contract",
    }:
        return None

    # These callbacks take a schema editor explicitly; recording its commands
    # does not execute any SQL or initialize a real Django connection.
    editor = MagicMock()
    cursor = editor.connection.cursor.return_value.__enter__.return_value
    statements: list[str] = []

    def execute(statement: str, params: list[object] | None = None) -> None:
        statements.append(statement)
        if statement.startswith("SELECT c.relname") and params is not None:
            # Simulate the expected FORCE-RLS state for the insurance callback.
            tables = params[0]
            assert isinstance(tables, list)
            cursor.fetchall.return_value = [
                (table,) for table in tables if table != "identity_user"
            ]

    editor.execute.side_effect = execute
    cursor.execute.side_effect = execute
    # Empty legacy table: still exercise disable/restore and GUC handling.
    cursor.fetchone.side_effect = [(None,), None]
    code(MagicMock(), editor)
    return statements


def migration_sql(migration: migrations.Migration) -> list[list[int | str]]:
    """Record SQL and reverse identity in original database-operation order."""
    entries: list[list[int | str]] = []
    for position, operation in enumerate(_operations(migration.operations)):
        if isinstance(operation, migrations.RunSQL):
            entries.append(
                [position, _digest(operation.sql), _digest(operation.reverse_sql)]
            )
        elif isinstance(operation, migrations.RunPython):
            script = _python_sql(operation)
            if script is not None:
                entries.append(
                    [
                        position,
                        _digest(script),
                        _digest(getattr(operation.reverse_code, "__qualname__", None)),
                    ]
                )
    return entries


def test_migration_sql_bytes_and_operation_order_match_pinned_source() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["source"] == BASE
    current = first_party_migrations()
    actual = {key: migration_sql(current[key]) for key in fixture["added"]}
    assert actual == fixture["added"]


def test_added_migrations_do_not_embed_policy_sql_best_effort() -> None:
    """Scan Python literals, not runtime-generated or externally imported SQL."""
    legacy = set(json.loads(FIXTURE.read_text(encoding="utf-8"))["legacy"])
    offenders: list[str] = []
    for key, migration in sorted(first_party_migrations().items()):
        if key in legacy:
            continue
        module = import_module(type(migration).__module__)
        assert module.__file__ is not None
        path = Path(module.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }
        offenders.extend(
            f"{key}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and POLICY_DDL.search(node.value)
        )
    assert not offenders, f"Move policy/guard SQL to a companion: {offenders}"
