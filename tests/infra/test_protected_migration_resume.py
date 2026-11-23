"""Interrupt real PostgreSQL commits and retry without faking a migration row."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from django.db import connections, transaction
from django.db.backends.postgresql.base import DatabaseWrapper
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from psycopg import sql

from patients.test_intake_migrations import (
    _default_connection,
    _scratch_database,
    _scratch_tenant_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.migrations.state import ProjectState
    from pytest_django.plugin import DjangoDbBlocker

type Target = tuple[str, str]

INTAKE = ("intake", "0013_patient_demographics")
AI = ("ai", "0003_artifact_retention")

# Use deparsed definitions, not OIDs or FK implementation-trigger names:
# each database allocates its own object identities.
CATALOG = """
WITH rels AS (
    SELECT * FROM pg_class WHERE relnamespace = 'clinic_app'::regnamespace
), facts AS (
    SELECT 'relation', relname::text,
           jsonb_build_array(relkind, relrowsecurity, relforcerowsecurity,
                             pg_get_userbyid(relowner), relacl::text) AS definition
    FROM rels WHERE relkind IN ('r', 'p', 'v', 'm', 'S')
    UNION ALL
    SELECT 'column', relname || '.' || attname,
           jsonb_build_array(format_type(atttypid, atttypmod), attnotnull,
                             attacl::text, pg_get_expr(adbin, adrelid))
    FROM pg_attribute JOIN rels ON attrelid = rels.oid
    LEFT JOIN pg_attrdef ON adrelid = attrelid AND adnum = attnum
    WHERE attnum > 0 AND NOT attisdropped AND relkind IN ('r', 'p', 'v', 'm')
    UNION ALL
    SELECT 'policy', tablename || '.' || policyname,
           jsonb_build_array(permissive, roles, cmd, qual, with_check)
    FROM pg_policies WHERE schemaname = 'clinic_app'
    UNION ALL
    SELECT 'constraint', relname || '.' || conname,
           jsonb_build_array(convalidated, pg_get_constraintdef(c.oid))
    FROM pg_constraint AS c JOIN rels ON conrelid = rels.oid
    UNION ALL
    SELECT 'trigger', relname || '.' || tgname,
           jsonb_build_array(tgenabled, pg_get_triggerdef(t.oid))
    FROM pg_trigger AS t JOIN rels ON tgrelid = rels.oid WHERE NOT tgisinternal
    UNION ALL
    SELECT 'index', rels.relname || '.' || i.relname,
           jsonb_build_array(indisvalid, pg_get_indexdef(indexrelid))
    FROM pg_index JOIN rels ON indrelid = rels.oid
    JOIN pg_class AS i ON i.oid = indexrelid
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text,
           jsonb_build_array(pg_get_functiondef(p.oid),
                             pg_get_userbyid(proowner), proacl::text)
    FROM pg_proc AS p WHERE pronamespace = 'clinic_app'::regnamespace
     AND prokind IN ('f', 'p')
)
SELECT * FROM facts ORDER BY 1, 2, 3
"""


class SimulatedInterruptionError(Exception):
    """Simulated process death after a committed database change."""


@contextmanager
def _clone(admin_url: str, source: DatabaseWrapper) -> Iterator[DatabaseWrapper]:
    name = f"mig_resume_{uuid4().hex[:12]}"
    source.close()  # A database serving as a template must have no connections.
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER clinic_owner TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(source.settings_dict["NAME"])
            )
        )
        clone = DatabaseWrapper(
            {**source.settings_dict, "NAME": name}, alias="migration_resume"
        )
        connections[clone.alias] = clone
        try:
            with _default_connection(clone):
                yield clone
        finally:
            clone.close()
            del connections[clone.alias]
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def _catalog(db: DatabaseWrapper) -> list[tuple[object, ...]]:
    with db.cursor() as cursor:
        cursor.execute(CATALOG)
        results: list[tuple[object, ...]] = cursor.fetchall()
    return results


def _seed(
    db: DatabaseWrapper, state: ProjectState, target: Target
) -> Callable[[DatabaseWrapper], tuple[object, ...]]:
    """Write a protected row through the pre-migration authorities."""
    registry = state.apps
    org_id = uuid4()
    with transaction.atomic(using=db.alias), db.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [str(org_id)]
        )
        registry.get_model("identity", "Organization").objects.create(
            id=org_id, name="Synthetic Migration Resume", cnpj="00000000000001"
        )
        _scratch_tenant_key(db, org_id)
        if target == INTAKE:
            row = registry.get_model("intake", "Patient").objects.create(
                organization_id=org_id,
                full_name="Synthetic Patient",
                birth_date=date(2001, 1, 1),
            )
            columns = "full_name, birth_date"
            table = "intake_patient"
        else:
            clinic = registry.get_model("identity", "Clinic").objects.create(
                organization_id=org_id,
                name="Synthetic Clinic",
                crm_uf="SP",
                timezone="America/Sao_Paulo",
            )
            user = registry.get_model("identity", "User").objects.create(
                username="synthetic-resume"
            )
            registry.get_model("identity", "UserClinicRole").objects.create(
                user_id=user.pk,
                clinic_id=clinic.pk,
                organization_id=org_id,
                role="owner",
            )
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(user.pk)]
            )
            row = registry.get_model("ai", "AIArtifact").objects.create(
                organization_id=org_id,
                clinic_id=clinic.pk,
                user_id=user.pk,
                purpose="front_desk",
                retention_class="transient_input",
                content="Synthetic AI fixture",
                expires_at=timezone.now() + timedelta(days=1),
            )
            columns = "content, retention_class"
            table = "ai_aiartifact"

    def read_bytes(connection: DatabaseWrapper) -> tuple[object, ...]:
        with transaction.atomic(using=connection.alias), connection.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.current_tenant', %s, true)", [str(org_id)]
            )
            cur.execute(
                sql.SQL("SELECT {} FROM clinic_app.{} WHERE id = %s").format(
                    sql.SQL(columns), sql.Identifier(table)
                ),
                [row.pk],
            )
            result: tuple[object, ...] | None = cur.fetchone()
        assert result is not None
        assert result[0]
        return result

    return read_bytes


def _apply_with_cut(
    db: DatabaseWrapper,
    target: Target,
    monkeypatch: pytest.MonkeyPatch,
    stop: int | None = None,
) -> list[str]:
    """Count real commits and autocommitted writes, including the baseline DDL."""
    executor = MigrationExecutor(db)
    assert [(m.app_label, m.name) for m, _ in executor.migration_plan([target])] == [
        target
    ]
    boundaries: list[str] = []

    def boundary(label: str) -> None:
        boundaries.append(label)
        if len(boundaries) == stop:
            raise SimulatedInterruptionError(label)

    original_commit = db.commit

    def commit() -> None:
        original_commit()
        boundary("transaction commit")

    def execute(
        run: Callable[..., object],
        statement: object,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        result = run(statement, params, many, context)
        command = str(statement).lstrip().upper()
        if (
            db.get_autocommit()
            and not command.startswith('INSERT INTO "DJANGO_MIGRATIONS"')
            and command.startswith(("ALTER ", "BEGIN", "CREATE ", "DROP ", "DO "))
        ):
            boundary(command.split()[0])
        return result

    with monkeypatch.context() as patch, db.execute_wrapper(execute):
        patch.setattr(db, "commit", commit)
        executor.migrate([target])
    return boundaries


@pytest.mark.parametrize("target", [INTAKE, AI], ids=["intake-0013", "ai-0003"])
def test_resume_after_each_committed_boundary(
    target: Target, django_db_blocker: DjangoDbBlocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_url = os.environ["TEST_SUPERUSER_DATABASE_URL"]
    with (
        django_db_blocker.unblock(),
        _scratch_database(admin_url) as template,
        _default_connection(template),
    ):
        executor = MigrationExecutor(template)
        parents = sorted(
            node.key for node in executor.loader.graph.node_map[target].parents
        )
        before_posture = executor.migrate(parents)
        read_bytes = _seed(template, before_posture, target)
        protected_bytes = read_bytes(template)

        with _clone(admin_url, template) as uninterrupted:
            boundaries = _apply_with_cut(uninterrupted, target, monkeypatch)
            expected = _catalog(uninterrupted)
            assert read_bytes(uninterrupted) == protected_bytes
            assert expected

        assert boundaries
        for stop in range(1, len(boundaries) + 1):
            with _clone(admin_url, template) as interrupted:
                with pytest.raises(SimulatedInterruptionError):
                    _apply_with_cut(interrupted, target, monkeypatch, stop)
                # Reopen without any session or Python checkpoint.
                interrupted.close()
                executor = MigrationExecutor(interrupted)
                assert target not in executor.loader.applied_migrations
                _apply_with_cut(interrupted, target, monkeypatch)
                executor = MigrationExecutor(interrupted)
                assert target in executor.loader.applied_migrations
                assert _catalog(interrupted) == expected, (target, stop)
                assert read_bytes(interrupted) == protected_bytes
                migration = executor.loader.get_migration(*target)
                with (
                    interrupted.schema_editor(atomic=False) as editor,
                    pytest.raises(IrreversibleError),
                ):
                    migration.unapply(before_posture.clone(), editor)
                assert _catalog(interrupted) == expected
