"""Catalog drift that is visible to a reader of a historical row or row type."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.tenancy.migrations._resume_catalog import FIXTURE
from django.db import transaction
from django.db.migrations.executor import MigrationExecutor

from infra import test_protected_migration_resume as resume
from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from django.db.backends.postgresql.base import DatabaseWrapper
    from pytest_django.plugin import DjangoDbBlocker


def _unpurged(db: DatabaseWrapper, org: UUID) -> bool:
    with transaction.atomic(using=db.alias), db.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_tenant', %s, true)", [str(org)])
        cursor.execute("SELECT purged_at IS NULL FROM clinic_app.ai_aiartifact")
        row = cursor.fetchone()
        assert row is not None
        return row[0] is True


def _names(db: DatabaseWrapper) -> tuple[str, ...]:
    with db.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.intake_patientdemographics LIMIT 0")
        return tuple(column[0] for column in cursor.description)


@pytest.mark.parametrize(
    "target", [resume.AI, resume.INTAKE], ids=["fast-default", "ordinal"]
)
def test_resume_preserves_preexisting_row_semantics(
    target: resume.Target,
    django_db_blocker: DjangoDbBlocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        executor = MigrationExecutor(db)
        state = executor.migrate(
            sorted(n.key for n in executor.loader.graph.node_map[target].parents)
        )
        org = uuid4()
        read_bytes = resume._seed(db, state, target, organization_id=org)
        ciphertext = read_bytes(db)
        with pytest.raises(
            resume.SimulatedInterruptionError, match="transaction commit"
        ):
            resume._apply_with_cut(db, target, monkeypatch, 1)
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        if target == resume.AI:
            assert _unpurged(db, org)
            with db.cursor() as cursor:
                cursor.execute("""
                    ALTER TABLE clinic_app.ai_aiartifact DROP COLUMN purged_at;
                    ALTER TABLE clinic_app.ai_aiartifact ADD purged_at timestamptz
                        DEFAULT '2001-01-01T00:00:00Z';
                    ALTER TABLE clinic_app.ai_aiartifact ALTER purged_at DROP DEFAULT;
                """)
                cursor.execute("""
                    SELECT atthasmissing, atthasdef FROM pg_attribute
                    WHERE attrelid='clinic_app.ai_aiartifact'::regclass
                      AND attname='purged_at'
                """)
                assert cursor.fetchone() == (True, False)
            assert not _unpurged(db, org)
            diagnostic = r"ai_aiartifact\.purged_at\.atthasmissing"
        else:
            assert _names(db)[2:4] == ("legal_name", "social_name")
            with db.cursor() as cursor:
                cursor.execute("""
                    ALTER TABLE clinic_app.intake_patientdemographics
                        RENAME legal_name TO swap_empty;
                    ALTER TABLE clinic_app.intake_patientdemographics
                        RENAME social_name TO legal_name;
                    ALTER TABLE clinic_app.intake_patientdemographics
                        RENAME swap_empty TO social_name;
                """)
            assert _names(db)[2:4] == ("social_name", "legal_name")
            diagnostic = (
                r"intake_patientdemographics\.(legal_name|social_name)\.position"
            )
        db.close()
        with pytest.raises(RuntimeError, match=diagnostic):
            MigrationExecutor(db).migrate([target])
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        assert read_bytes(db) == ciphertext
        if target == resume.AI:
            assert not _unpurged(db, org)
        else:
            assert _names(db)[2:4] == ("social_name", "legal_name")


def test_freshness_rejects_reordered_reference(
    tmp_path: Path, django_db_blocker: DjangoDbBlocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = json.loads(FIXTURE.read_bytes())
    history = fixture["history"]
    fixture["history"] = dict(sorted(history.items()))
    assert list(fixture["history"]) != list(history)
    reordered = tmp_path / "reordered.json"
    reordered.write_text(json.dumps(fixture, indent=1) + "\n", encoding="utf-8")
    monkeypatch.setattr(resume, "FIXTURE", reordered)
    with pytest.raises(AssertionError, match="Fresh catalog reference differs"):
        resume.test_committed_fingerprints_match_fresh_migration_catalog(
            django_db_blocker
        )
