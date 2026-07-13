from typing import Final

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, UserClinicRole
from apps.tenancy.models import TenantScopedModel
from django.apps import apps as django_apps
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

EXPECTED_TENANT_COLUMNS: Final = {
    "identity_organization": "id",
    "identity_clinic": "organization_id",
    "identity_userclinicrole": "organization_id",
    "tenancy_tenantprobe": "organization_id",
}


def test_all_concrete_tenant_models_have_the_exact_rls_policy_set() -> None:
    # Given: every concrete TenantScopedModel plus the Organization tenant root
    tenant_models = {
        model._meta.db_table
        for model in django_apps.get_models()
        if issubclass(model, TenantScopedModel) and not model._meta.abstract
    }
    tenant_models.update(
        model._meta.db_table for model in (Organization, Clinic, UserClinicRole)
    )

    # When: table flags and the tenant-table policies are read
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT class.relname, class.relrowsecurity, class.relforcerowsecurity,
                   class.relowner::regrole::text
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = 'clinic_app'
              AND class.relname = ANY(%s)
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        table_posture = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT tablename, policyname
            FROM pg_catalog.pg_policies
            WHERE schemaname = 'clinic_app'
              AND tablename = ANY(%s)
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        policies = set(cursor.fetchall())

    # Then: missing models, missing policies, and extra policies all fail
    assert tenant_models == set(EXPECTED_TENANT_COLUMNS)
    assert table_posture == {
        (table, True, True, "clinic_owner") for table in EXPECTED_TENANT_COLUMNS
    }
    assert policies == {
        (table, "tenant_isolation") for table in EXPECTED_TENANT_COLUMNS
    }


def test_tenant_policies_are_public_permissive_all_and_fail_closed() -> None:
    # Given: the four expected RLS policy targets
    # When: PostgreSQL deparses every policy expression
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT policies.tablename, policies.permissive, policies.roles,
                   policies.cmd, policies.qual, policies.with_check
            FROM pg_catalog.pg_policies AS policies
            WHERE policies.schemaname = 'clinic_app'
              AND policies.tablename = ANY(%s)
            ORDER BY policies.tablename
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        policy_rows = cursor.fetchall()

    # Then: each policy has identical fail-closed USING and WITH CHECK clauses
    assert len(policy_rows) == len(EXPECTED_TENANT_COLUMNS)
    for table, permissive, roles, command, using, with_check in policy_rows:
        tenant_column = EXPECTED_TENANT_COLUMNS[table]
        expected_expression = (
            f"({tenant_column} = (NULLIF(current_setting("
            "'app.current_tenant'::text, true), ''::text))::uuid)"
        )
        assert permissive == "PERMISSIVE"
        assert roles == ["public"]
        assert command == "ALL"
        assert using == expected_expression
        assert with_check == expected_expression


def test_runtime_role_and_tenant_table_privileges_are_exact(
    app_database_url: str,
) -> None:
    # Given: a runtime connection and the expected tenant-table DML surface
    with psycopg.connect(app_database_url) as app_connection:
        runtime_posture = app_connection.execute(
            """
            SELECT current_user, role.rolsuper, role.rolbypassrls,
                   role.rolcreatedb
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = current_user
            """
        ).fetchone()

    # When: explicit table and sensitive-user grants are enumerated
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app'
              AND table_schema = 'clinic_app'
              AND table_name = ANY(%s)
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        tenant_grants = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT privilege_type FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'identity_user'
            UNION ALL
            SELECT privilege_type FROM information_schema.role_column_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'identity_user'
            """
        )
        user_grants = cursor.fetchall()
        cursor.execute(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb
            FROM pg_catalog.pg_roles
            WHERE rolname = ANY(%s)
            """,
            [["clinic_owner", "clinic_app", "clinic_resolver", "clinic_super"]],
        )
        role_posture = set(cursor.fetchall())

    # Then: app is ordinary, tenant DML is narrow, and User is fully denied
    assert runtime_posture == ("clinic_app", False, False, False)
    assert tenant_grants == {
        (table, privilege)
        for table in EXPECTED_TENANT_COLUMNS
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }
    assert user_grants == []
    assert role_posture == {
        ("clinic_owner", True, False, False, False),
        ("clinic_app", True, False, False, False),
        ("clinic_resolver", False, False, True, False),
        ("clinic_super", True, True, False, False),
    }
