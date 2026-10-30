"""Source and migration-state checks; no database connections."""

from datetime import UTC, datetime, timedelta
from inspect import signature
from uuid import uuid4

import pytest
from apps.core.telemetry import MetricsRegistry
from apps.identity.models import ClinicConfiguration
from apps.retention import execution
from apps.retention.models import ArtifactPurge
from apps.retention.services import (
    RetentionAccessDeniedError,
    apply_retention_policy,
)
from apps.scribe.presenter import CapturePanel
from config.celery import app
from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.template.loader import render_to_string
from django.utils.translation import gettext, override


def test_synthetic_defaults_are_seven_days_and_non_destructive() -> None:
    assert ClinicConfiguration._meta.get_field("raw_audio_retention_days").default == 7
    assert ClinicConfiguration._meta.get_field("retention_dry_run").default is True
    assert ClinicConfiguration._meta.get_field("retention_backup_days").default == 35


@pytest.mark.parametrize(
    "record_class", ["ehr.document_version", "ehr.encounter", "unknown"]
)
def test_apply_rejects_every_non_transient_class_before_any_database_lookup(
    record_class: str,
) -> None:
    with pytest.raises(RetentionAccessDeniedError):
        apply_retention_policy(clinic_id=uuid4(), record_class=record_class)


def test_public_service_does_not_accept_actor_tenant_or_dry_run_overrides() -> None:
    assert list(signature(apply_retention_policy).parameters) == [
        "clinic_id",
        "record_class",
    ]
    assert execution.TRANSIENT_CLASSES == (
        "raw_audio",
        "ai_artifact",
        "transcript_partial",
    )


def test_retention_model_state_matches_migrations_without_database_io() -> None:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    with override("en"):
        changes = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(apps),
            NonInteractiveMigrationQuestioner(),
        ).changes(graph=loader.graph)
    assert not {
        key: changes[key]
        for key in ("retention", "identity", "scribe", "ai")
        if key in changes
    }


def test_workers_discover_both_retention_tasks_without_database_io() -> None:
    app.autodiscover_tasks(force=True)
    assert "apps.retention.tasks.apply_policy" in app.tasks
    assert "apps.retention.tasks.purge_artifact" in app.tasks


def test_purge_failure_metric_has_no_tenant_subject_or_error_labels() -> None:
    registry = MetricsRegistry()
    registry.record_retention_failure()
    assert (
        "clinic_retention_purge_failures_total 1\n"
        in registry._render_retention_failures()
    )
    assert "{" not in registry._render_retention_failures()


def test_capture_renders_the_dated_purge_receipt_without_loading_audio() -> None:
    completed = datetime(2026, 10, 26, 12, tzinfo=UTC)
    receipt = ArtifactPurge(
        state="purged",
        purged_at=completed,
        backup_copy_expires_at=completed + timedelta(days=35),
    )
    html = render_to_string(
        "scribe/capture.html",
        {
            "clinic_id": uuid4(),
            "capture": CapturePanel(available=False, purge_receipts=(receipt,)),
        },
    )
    assert "data-audio-purge-receipt" in html
    assert gettext("Audio discarded according to policy.") in html
    assert gettext("Backup copy expires at") in html
    assert "26/10/2026" in html
    assert "30/11/2026" in html
