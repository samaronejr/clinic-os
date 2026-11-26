"""The numbered migration delegates its executable SQL to its topic module."""

from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path

from django.db import migrations


def test_numbered_refinement_migration_installs_the_imported_topic_sql() -> None:
    migration = import_module("apps.billing.migrations.0017_psp_unknown_refinement")
    assert migration.__file__ is not None
    source = ast.parse(Path(migration.__file__).read_text(encoding="utf-8"))
    [operation] = migration.Migration.operations
    assert isinstance(operation, migrations.RunSQL)
    topics = [
        import_module(node.module)
        for node in source.body
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("apps.billing.migrations._")
        and node.module.endswith("_sql")
    ]
    assert any(getattr(topic, "SQL", None) is operation.sql for topic in topics)
    assert migration.Migration.dependencies == [("billing", "0016_psp_observer")]
