"""Require the F6 atomic exception to create, not mutate, tables."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import sqlparse
from apps.tenancy.migrations._resumable_schema import (
    AtomicSchemaMigration,
    GuardedMigration,
)
from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState

from infra.test_migration_sql_layout import _operations, _python_sql

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.db.migrations.operations.base import Operation

_IDENT = r'(?:"(?:[^"]|"")+"|[a-z_][\w$]*)'
_RELATION = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"
_RELATIONS = re.compile(_RELATION, re.IGNORECASE)
_TARGETS = re.compile(
    r"\A(?:"
    r"(?:ALTER TABLE|INSERT INTO|UPDATE|DELETE FROM|MERGE INTO|"
    r"TRUNCATE(?: TABLE)?|DROP TABLE|LOCK(?: TABLE)?)\s+(?:IF EXISTS\s+)?"
    r"|(?:CREATE(?: OR REPLACE)?|ALTER|DROP)\s+(?:CONSTRAINT\s+)?"
    rf"(?:POLICY|TRIGGER)\b[^;]+?\bON\s+"
    r"|CREATE\s+(?:UNIQUE\s+)?INDEX\b[^;]+?\bON\s+"
    r")(?:ONLY\s+)?"
    rf"(?P<targets>{_RELATION}(?:\s*,\s*{_RELATION})*)",
    re.IGNORECASE,
)
_TABLE_ACL = re.compile(
    r"\A(?:GRANT|REVOKE)\b.+?\bON\s+(?:TABLE\s+)?"
    rf"(?P<targets>{_RELATION}(?:\s*,\s*{_RELATION})*)\s+(?:TO|FROM)\s+",
    re.IGNORECASE,
)


def _sql_scripts(operation: migrations.RunSQL | migrations.RunPython) -> Iterator[str]:
    if isinstance(operation, migrations.RunPython):
        scripts = _python_sql(operation)
        assert isinstance(scripts, list), "cannot inspect atomic RunPython effects"
        yield from scripts
        return
    if isinstance(operation.sql, str):
        yield operation.sql
        return
    assert isinstance(operation.sql, (tuple, list)), "unknown atomic RunSQL shape"
    for item in operation.sql:
        statement = item[0] if isinstance(item, (list, tuple)) else item
        assert isinstance(statement, str)
        yield statement


def _sql_targets(scripts: Iterable[str]) -> Iterator[str]:
    for script in scripts:
        # Dollar-quoted function bodies stay in their CREATE FUNCTION
        # statements; the bodies don't run during installation.
        for raw in sqlparse.split(sqlparse.format(script, strip_comments=True)):
            statement = " ".join(raw.split())
            assert not re.match(r"(?:DO|CALL|WITH)\b", statement, re.IGNORECASE), (
                "cannot verify table targets of an indirect atomic SQL operation"
            )
            match = _TARGETS.match(statement) or _TABLE_ACL.match(statement)
            if match:
                for relation in _RELATIONS.findall(match["targets"]):
                    name = relation.rsplit(".", 1)[-1].strip()
                    yield (
                        name[1:-1].replace('""', '"')
                        if name.startswith('"')
                        else name.lower()
                    )


def _earlier_targets(
    operations: Iterable[Operation],
    app: str,
    models: set[tuple[str, str]],
    earlier_tables: set[str],
) -> set[str]:
    targets: set[str] = set()
    for operation in _operations(operations):
        if isinstance(operation, migrations.CreateModel):
            continue
        if isinstance(operation, (migrations.RunSQL, migrations.RunPython)):
            targets.update(set(_sql_targets(_sql_scripts(operation))) & earlier_tables)
            continue
        model_name = getattr(operation, "model_name", None)
        assert isinstance(model_name, str), (
            f"unclassified atomic operation: {operation}"
        )
        if (app, model_name.lower()) in models:
            targets.add(f"{app}.{model_name.lower()}")
    return targets


def test_atomic_lane_migrations_do_not_mutate_earlier_tables() -> None:
    loader = MigrationLoader(None)
    graph = loader.graph
    ordered = dict.fromkeys(
        key for leaf in graph.leaf_nodes() for key in graph.forwards_plan(leaf)
    )
    state = ProjectState(real_apps=loader.unmigrated_apps)
    offenders: dict[str, list[str]] = {}
    for app, name in ordered:
        migration = graph.nodes[app, name]
        if migration.atomic and isinstance(
            migration, (AtomicSchemaMigration, GuardedMigration)
        ):
            older_tables = {
                model.options.get("db_table", f"{label}_{model.name_lower}")
                for (label, _), model in state.models.items()
            }
            targets = _earlier_targets(
                migration.operations, app, set(state.models), older_tables
            )
            if targets:
                offenders[f"{app}.{name}"] = sorted(targets)
        state = migration.mutate_state(state, preserve=False)
    assert not offenders, (
        f"Atomic F6 migrations target pre-existing tables: {offenders}"
    )
