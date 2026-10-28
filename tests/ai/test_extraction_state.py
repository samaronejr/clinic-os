"""Disk-only extraction migration and model-state checks."""

from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState


def test_extraction_schema_is_complete_without_database_io() -> None:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    changes = MigrationAutodetector(
        loader.project_state(),
        ProjectState.from_apps(apps),
        NonInteractiveMigrationQuestioner(),
    ).changes(graph=loader.graph)
    assert not changes.get("ai")
    assert not changes.get("orders")
    migration = loader.disk_migrations[("ai", "0003_document_extraction")]
    assert not migration.atomic
    assert not migration.operations[0].reversible
