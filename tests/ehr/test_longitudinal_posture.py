"""Exact live posture for the new clinical tables and restricted source overlay."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import DataError, connection, migrations, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from psycopg.errors import DivisionByZero

from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import ProjectState

pytestmark = pytest.mark.django_db(transaction=True)

POLICIES = {
    "ehr_medicationstatement": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("longitudinal_read", "SELECT", "clinic_app"),
        ("longitudinal_insert", "INSERT", "clinic_app"),
        ("longitudinal_confirm", "UPDATE", "clinic_app"),
    },
    "ehr_observation": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("longitudinal_read", "SELECT", "clinic_app"),
        ("longitudinal_insert", "INSERT", "clinic_app"),
        ("observation_nursing_insert", "INSERT", "clinic_app"),
        ("longitudinal_confirm", "UPDATE", "clinic_app"),
    },
    "ehr_restrictednote": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("note_restriction_read", "SELECT", "clinic_app"),
        ("note_restriction_insert", "INSERT", "clinic_app"),
    },
    "ehr_noteaccessgrant": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("note_grant_read", "SELECT", "clinic_app"),
        ("note_grant_insert", "INSERT", "clinic_app"),
        ("note_grant_revoke", "UPDATE", "clinic_app"),
    },
    "ehr_copiednoteblock": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("copy_read", "SELECT", "clinic_app"),
        ("copy_insert", "INSERT", "clinic_app"),
        ("copy_review", "UPDATE", "clinic_app"),
    },
    "ehr_timelinerefresh": {("setup_tenant", "ALL", "clinic_owner")},
    "ehr_timelineprojection": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("timeline_read", "SELECT", "clinic_app"),
    },
}


@pytest.mark.parametrize("table", POLICIES)
def test_exact_clinical_policy_and_force_rls(table: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid=('clinic_app.'||%s)::regclass",
            [table],
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute(
            "SELECT policyname,cmd,roles,permissive FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=%s",
            [table],
        )
        rows = cursor.fetchall()
        assert {
            (name, command, roles[0]) for name, command, roles, _mode in rows
        } == POLICIES[table]
        assert all(
            len(roles) == 1 and mode == "PERMISSIVE"
            for _name, _command, roles, mode in rows
        )
        cursor.execute(
            "SELECT has_table_privilege('clinic_app',"
            "('clinic_app.'||%s)::regclass,'DELETE'),"
            "has_table_privilege('clinic_agent',"
            "('clinic_app.'||%s)::regclass,'SELECT,INSERT,UPDATE,DELETE')",
            [table, table],
        )
        assert cursor.fetchone() == (False, False)


def test_restriction_applies_to_source_rows_before_any_count() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL search_path=pg_catalog")
        cursor.execute(
            "SELECT permissive,roles,cmd,qual,with_check FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename='ehr_clinicaldocumentversion' "
            "AND policyname='restricted_note_visibility'"
        )
        assert cursor.fetchone() == (
            "RESTRICTIVE",
            ["clinic_app"],
            "ALL",
            "clinic_app.ehr_note_visible(document_id)",
            "clinic_app.ehr_note_visible(document_id)",
        )


def test_enveloped_columns_have_no_plaintext_shadows() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name,column_name,data_type FROM information_schema.columns "
            "WHERE table_schema='clinic_app' AND table_name IN "
            "('ehr_medicationstatement','ehr_observation') "
            "AND column_name IN ('details','value','details_legacy','value_legacy')"
        )
        assert set(cursor.fetchall()) == {
            ("ehr_medicationstatement", "details", "bytea"),
            ("ehr_observation", "value", "bytea"),
        }


def test_longitudinal_contract_failure_rolls_back_new_schema(
    superuser_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ("ehr", "0014_longitudinal_record")
    previous = ("ehr", "0013_template_v2")
    new_tables = {
        "ehr_restrictednote",
        "ehr_noteaccessgrant",
        "ehr_copiednoteblock",
        "ehr_observation",
        "ehr_medicationstatement",
        "ehr_timelineprojection",
        "ehr_timelinerefresh",
    }
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        executor = MigrationExecutor(connection)
        parents = [
            node
            for node in executor.loader.graph.forwards_plan(target)
            if node != target
        ]
        executor.migrate(parents)
        executor = MigrationExecutor(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='clinic_app'"
            )
            before = {row[0] for row in cursor.fetchall()}
        assert "ehr_episodeencounter" in before
        assert before.isdisjoint(new_tables)
        database_forwards = migrations.RunSQL.database_forwards

        def fail_contract(
            operation: migrations.RunSQL,
            app_label: str,
            schema_editor: BaseDatabaseSchemaEditor,
            from_state: ProjectState,
            to_state: ProjectState,
        ) -> None:
            database_forwards(operation, app_label, schema_editor, from_state, to_state)
            with schema_editor.connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")

        with monkeypatch.context() as failing:
            failing.setattr(migrations.RunSQL, "database_forwards", fail_contract)
            with pytest.raises(DataError) as refused:
                executor.migrate([target])
            assert isinstance(refused.value.__cause__, DivisionByZero)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='clinic_app'"
            )
            assert {row[0] for row in cursor.fetchall()} == before
        assert target not in executor.recorder.applied_migrations()

        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor = MigrationExecutor(connection)
        assert executor.loader.disk_migrations[target].atomic is False
        with pytest.raises(IrreversibleError):
            executor.migrate([previous])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='clinic_app'"
            )
            assert {row[0] for row in cursor.fetchall()} == before | new_tables
