"""Static graph and model-state checks; no database or migration execution."""

import json
import os
import sys
from pathlib import Path

from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.utils.translation import override
from ops.testing import renewal_runner

from identity.legacy_guard_inventory import discover


def test_gateway_migration_state_matches_the_models_without_database_io() -> None:
    # Given: disk migrations and the installed model state, without a connection.
    loader = MigrationLoader(None, ignore_no_migrations=True)
    # When: the same state differencing used by makemigrations is performed.
    with override("en"):
        changes = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(apps),
            NonInteractiveMigrationQuestioner(),
        ).changes(graph=loader.graph)
    # Then: no gateway or configuration change is missing from migrations.
    assert not changes.get("ai")
    assert not changes.get("identity")


def test_python_authority_manifest_matches_every_source_candidate() -> None:
    # Given: the same reviewed manifest used by the executed DB census.
    inventory = json.loads(
        (Path(__file__).parents[1] / "identity" / "legacy_guards.json").read_text()
    )
    # When/Then: no newly added Python authority site is silently omitted.
    assert discover() == {
        row["symbol"]: row["signals"] for row in inventory["candidates"]
    }


def test_fresh_worker_discovers_agent_execution_without_database_io(
    tmp_path: Path,
) -> None:
    script = """
import django
from unittest.mock import patch
with patch(
    "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
    side_effect=AssertionError("startup attempted database I/O"),
):
    django.setup()
    from config.celery import app
    app.autodiscover_tasks(force=True)
    assert "apps.ai.tasks.execute_agent" in app.tasks
"""
    log = tmp_path / "worker-registration.log"
    result = renewal_runner._run_bounded(
        [sys.executable, "-c", script],
        {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"},
        log,
        timeout=30,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result == 0, log.read_text()
