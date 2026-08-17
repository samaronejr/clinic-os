from importlib import import_module

import pytest
from django.db import connection


@pytest.mark.django_db(transaction=True)
def test_appointment_has_exact_force_rls_and_column_acl_catalog() -> None:
    module = import_module("apps.scheduling.rls")
    appointment_targets = getattr(module, "APPOINTMENT_RLS_TARGETS", None)
    assert appointment_targets == frozenset(
        {("scheduling_appointment", "organization_id")}
    )
    assert module.SCHEDULING_RLS_TARGETS | appointment_targets == frozenset(
        {
            ("scheduling_appointment", "organization_id"),
            ("scheduling_availabilityblock", "organization_id"),
        }
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity, relowner::regrole::text "
            "FROM pg_catalog.pg_class WHERE oid = "
            "'clinic_app.scheduling_appointment'::regclass"
        )
        assert cursor.fetchone() == (True, True, "clinic_owner")
        cursor.execute(
            "SELECT policyname, permissive, roles, cmd, qual, with_check "
            "FROM pg_catalog.pg_policies WHERE schemaname = 'clinic_app' "
            "AND tablename = 'scheduling_appointment'"
        )
        expression = (
            "(organization_id = (NULLIF(current_setting("
            "'app.current_tenant'::text, true), ''::text))::uuid)"
        )
        assert cursor.fetchone() == (
            "tenant_isolation",
            "PERMISSIVE",
            ["public"],
            "ALL",
            expression,
            expression,
        )
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND table_name = 'scheduling_appointment' ORDER BY privilege_type"
        )
        assert cursor.fetchall() == [("INSERT",), ("SELECT",)]
        cursor.execute(
            "SELECT column_name FROM information_schema.role_column_grants "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND table_name = 'scheduling_appointment' "
            "AND privilege_type = 'UPDATE' ORDER BY column_name"
        )
        assert cursor.fetchall() == [
            ("cancellation_reason",),
            ("cancelled_at",),
            ("end_at",),
            ("start_at",),
            ("status",),
            ("updated_at",),
        ]
        cursor.execute(
            "SELECT grantee, privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = 'clinic_app' "
            "AND table_name = 'scheduling_appointment' "
            "AND grantee = ANY(%s) ORDER BY grantee, privilege_type",
            [["PUBLIC", "clinic_resolver"]],
        )
        assert cursor.fetchall() == []
