"""Exact interop posture from the migrated catalog: RLS, grants, guards, links."""

from __future__ import annotations

from typing import Any

import pytest
from apps.interop.migrations import _import_sql
from apps.interop.migrations._schema import ProtectedSchema
from apps.interop.rls import CUSTOM_RLS_TABLES
from django.db import DatabaseError, connection, transaction
from django.db.migrations.loader import MigrationLoader

from patient_service_support import runtime_role

pytestmark = pytest.mark.django_db(transaction=True)
HIDDEN = "interop_hiddenenrollment"
TABLES = (*sorted(CUSTOM_RLS_TABLES), HIDDEN)
FK_SQL = """
SELECT DISTINCT c.conrelid::regclass::text, a.attname
FROM pg_catalog.pg_constraint c
CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS k(local_att, ref_att)
JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.local_att
JOIN pg_catalog.pg_attribute r ON r.attrelid=c.confrelid AND r.attnum=k.ref_att
WHERE c.contype='f' AND r.attname='id'
  AND c.confrelid IN ('clinic_app.intake_patient'::regclass,
                      'clinic_app.intake_patientclinicenrollment'::regclass)
"""


def _rows(sql: str, params: list[Any] | None = None) -> set[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or [])
        return {tuple(row) for row in cursor.fetchall()}


def test_import_tables_are_forced_rls_with_exact_policies() -> None:
    flags = _rows(
        "SELECT relname, relrowsecurity, relforcerowsecurity, relowner::regrole::text"
        " FROM pg_catalog.pg_class WHERE relname = ANY(%s)",
        [list(TABLES)],
    )
    assert flags == {(table, True, True, "clinic_owner") for table in TABLES}
    policies = _rows(
        "SELECT tablename, policyname, permissive, roles::text, cmd"
        " FROM pg_catalog.pg_policies WHERE tablename = ANY(%s)",
        [list(TABLES)],
    )
    assert policies == {
        *(
            entry
            for table in CUSTOM_RLS_TABLES
            for entry in (
                (table, "interop_owner", "PERMISSIVE", "{clinic_owner}", "ALL"),
                (table, "interop_read", "PERMISSIVE", "{clinic_app}", "SELECT"),
                (table, "interop_insert", "PERMISSIVE", "{clinic_app}", "INSERT"),
            )
        ),
        (
            "interop_importbatch",
            "interop_update",
            "PERMISSIVE",
            "{clinic_app}",
            "UPDATE",
        ),
        (HIDDEN, "tenant_isolation", "PERMISSIVE", "{public}", "ALL"),
    }


def test_intake_reads_hide_rolled_back_enrollments_restrictively() -> None:
    assert _rows(
        "SELECT tablename, permissive, roles::text, cmd, qual"
        " FROM pg_catalog.pg_policies WHERE policyname='interop_import_visibility'"
    ) == {
        (
            "intake_patientclinicenrollment",
            "RESTRICTIVE",
            "{clinic_app,clinic_owner}",
            "SELECT",
            "(NOT (EXISTS ( SELECT 1\n   FROM interop_hiddenenrollment h\n"
            "  WHERE (h.enrollment_id = intake_patientclinicenrollment.id))))",
        ),
        (
            "intake_duplicatecandidate",
            "RESTRICTIVE",
            "{clinic_app,clinic_owner}",
            "SELECT",
            "(NOT (EXISTS ( SELECT 1\n   FROM interop_hiddenenrollment h\n"
            "  WHERE (h.enrollment_id = ANY (ARRAY[intake_duplicatecandidate"
            ".enrollment_a_id, intake_duplicatecandidate.enrollment_b_id])))))",
        ),
    }


def test_runtime_and_resolver_grants_are_exact() -> None:
    runtime = _rows(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee='clinic_app' AND table_name = ANY(%s)",
        [list(TABLES)],
    )
    assert runtime == {
        *(
            (table, privilege)
            for table in CUSTOM_RLS_TABLES
            for privilege in ("SELECT", "INSERT")
            if table not in _import_sql.METADATA_COLUMNS or privilege == "INSERT"
        ),
        (HIDDEN, "SELECT"),
    }
    columns = _rows(
        "SELECT c.relname, a.attname, acl.privilege_type FROM pg_catalog.pg_class c"
        " JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid"
        " CROSS JOIN LATERAL aclexplode(a.attacl) AS acl"
        " WHERE c.relname = ANY(%s) AND acl.grantee='clinic_app'::regrole",
        [list(TABLES)],
    )
    assert columns == {
        *(
            ("interop_importbatch", column, "UPDATE")
            for column in ("state", "committed_count", "committed_at")
        ),
        *(
            (table, column, "SELECT")
            for table, names in _import_sql.METADATA_COLUMNS.items()
            for column in names
        ),
    }
    # Every column of the two payload tables is either metadata or payload.
    catalog = _rows(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema='clinic_app' AND table_name = ANY(%s)",
        [list(_import_sql.METADATA_COLUMNS)],
    )
    for table, names in _import_sql.METADATA_COLUMNS.items():
        payload = _import_sql.PAYLOAD_COLUMNS[table]
        assert set(names).isdisjoint(payload)
        assert {column for name, column in catalog if name == table} == {
            *names,
            *payload,
        }
    agent = _rows(
        "SELECT table_name, privilege_type"
        " FROM information_schema.role_table_grants"
        " WHERE grantee='clinic_agent' AND table_name = ANY(%s)",
        [list(TABLES)],
    )
    assert agent == set()
    resolver = _rows(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee='clinic_resolver' AND table_name = ANY(%s)",
        [[*TABLES, *_import_sql.RESOLVER_ACTIVITY_GRANTS]],
    )
    assert resolver == {
        *(
            (table, "SELECT")
            for table in (*TABLES, *_import_sql.RESOLVER_ACTIVITY_GRANTS)
        ),
        (HIDDEN, "INSERT"),
    }


def test_every_import_table_has_its_guard_and_truncate_refusal() -> None:
    triggers = _rows(
        "SELECT c.relname, t.tgname, p.proname, t.tgtype"
        " FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid"
        " JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid"
        " WHERE NOT t.tgisinternal AND c.relname = ANY(%s)",
        [list(TABLES)],
    )
    # tgtype bits: 1 row, 2 before, 4 insert, 8 delete, 16 update, 32 truncate.
    assert triggers == {
        entry
        for table in TABLES
        for entry in (
            (table, "interop_guard", "interop_guard", 1 | 2 | 4 | 8 | 16),
            (table, "interop_no_truncate", "interop_no_truncate", 2 | 32),
        )
    }


def test_every_patient_and_enrollment_reference_is_classified() -> None:
    """A new relation pointing at a patient fails until rollback can see it."""
    live = {
        (table.removeprefix("clinic_app."), column) for table, column in _rows(FK_SQL)
    }
    classified = {
        *_import_sql.PATIENT_ACTIVITY,
        *_import_sql.ENROLLMENT_ACTIVITY,
        *_import_sql.IMPORT_OWNED,
        *_import_sql.DERIVED,
    }
    assert live == classified
    assert len(classified) == sum(
        len(group)
        for group in (
            _import_sql.PATIENT_ACTIVITY,
            _import_sql.ENROLLMENT_ACTIVITY,
            _import_sql.IMPORT_OWNED,
            _import_sql.DERIVED,
        )
    )
    checked = {
        table
        for table, _column in (
            *_import_sql.PATIENT_ACTIVITY,
            *_import_sql.ENROLLMENT_ACTIVITY,
            *_import_sql.IMPORT_OWNED,
        )
    }
    readable = _rows(
        "SELECT relname FROM pg_catalog.pg_class WHERE relname = ANY(%s)"
        " AND has_table_privilege('clinic_resolver', oid, 'SELECT')",
        [sorted(checked)],
    )
    assert {row[0] for row in readable} == checked


def test_runtime_cannot_write_hide_markers_or_rewrite_imports() -> None:
    with runtime_role(), connection.cursor() as cursor:
        for statement in (
            "INSERT INTO clinic_app.interop_hiddenenrollment"
            " (id, organization_id, clinic_id, enrollment_id, job_id, hidden_at)"
            " VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),"
            " gen_random_uuid(), gen_random_uuid(), now())",
            "UPDATE clinic_app.interop_importedrecord SET kind='note'",
            "DELETE FROM clinic_app.interop_importjob",
        ):
            with pytest.raises(DatabaseError) as denied, transaction.atomic():
                cursor.execute(statement)
            assert getattr(denied.value.__cause__, "sqlstate", None) == "42501"


def test_initial_import_schema_is_irreversible() -> None:
    migration = MigrationLoader(None).disk_migrations[
        "interop", "0001_import_framework"
    ]
    assert migration.atomic is False
    assert [type(operation) for operation in migration.operations] == [ProtectedSchema]
    assert migration.operations[0].reversible is False
    assert all(
        (app, name) in MigrationLoader(None).graph.nodes
        for app, name in migration.dependencies
        if name != "__first__"
    )
