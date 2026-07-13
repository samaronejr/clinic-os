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
RESOLVER_SELECT_TABLES: Final = {
    "identity_user",
    "identity_organization",
    "identity_clinic",
    "identity_userclinicrole",
}
FUNCTION_SIGNATURES: Final = {
    ("auth_lookup", "requested_username text"),
    ("load_current_user", ""),
    ("user_has_org", "requested_org uuid"),
    ("user_organizations", ""),
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

    # When: table flags and all application-schema policies are read
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
            """
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
            ORDER BY policies.tablename
            """
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


def test_resolver_table_privileges_are_an_exact_select_allowlist() -> None:
    # Given: the BYPASSRLS resolver role's application-schema grants
    # When: every table and column privilege is enumerated
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_resolver' AND table_schema = 'clinic_app'
            UNION ALL
            SELECT '<default-table>', acl.privilege_type
            FROM pg_catalog.pg_default_acl AS defaults
            CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE defaults.defaclobjtype = 'r'
              AND grantee.rolname = 'clinic_resolver'
            """
        )
        table_grants = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT class.relname, attribute.attname, acl.privilege_type
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS class ON class.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'clinic_app'
              AND grantee.rolname = 'clinic_resolver'
            """
        )
        column_grants = cursor.fetchall()

    # Then: only four identity relations are readable and no DML path exists
    assert table_grants == {(table, "SELECT") for table in RESOLVER_SELECT_TABLES}
    assert column_grants == []


def test_resolver_functions_have_exact_hardened_catalog_posture() -> None:
    # Given: every function owned by the resolver role
    # When: signatures, execution ACLs, and safety attributes are inspected
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT procedure.proname,
                   pg_catalog.pg_get_function_identity_arguments(procedure.oid),
                   procedure.proowner::regrole::text,
                   procedure.prosecdef, procedure.provolatile,
                   procedure.proparallel, procedure.proconfig,
                   NOT EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )) AS acl
                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                   ),
                   pg_catalog.has_function_privilege(
                       'clinic_app', procedure.oid, 'EXECUTE'
                   ),
                   ARRAY(
                       SELECT COALESCE(grantee.rolname, 'PUBLIC')
                       FROM pg_catalog.aclexplode(COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )) AS acl
                       LEFT JOIN pg_catalog.pg_roles AS grantee
                         ON grantee.oid = acl.grantee
                       WHERE acl.privilege_type = 'EXECUTE'
                       ORDER BY 1
                   ),
                   procedure.prorows,
                   pg_catalog.pg_get_function_result(procedure.oid),
                   pg_catalog.pg_get_functiondef(procedure.oid)
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = 'clinic_app'
              AND procedure.proowner = 'clinic_resolver'::pg_catalog.regrole
            ORDER BY procedure.proname
            """
        )
        functions = cursor.fetchall()

    # Then: exactly four non-overloaded hardened definer functions exist
    assert {(row[0], row[1]) for row in functions} == FUNCTION_SIGNATURES
    assert len(functions) == 4
    for function in functions:
        name = function[0]
        assert function[2:9] == (
            "clinic_resolver",
            True,
            "s",
            "u",
            ["search_path=pg_catalog, clinic_app, pg_temp"],
            True,
            True,
        )
        assert function[9] == ["clinic_app", "clinic_resolver"]
        if name == "load_current_user":
            assert function[10] == 1.0
        assert "EXECUTE " not in function[12].upper()

    results = {row[0]: row[11] for row in functions}
    assert results == {
        "auth_lookup": (
            "TABLE(id uuid, username character varying, "
            "password character varying, is_active boolean)"
        ),
        "load_current_user": "SETOF identity_user",
        "user_has_org": "boolean",
        "user_organizations": "SETOF uuid",
    }
