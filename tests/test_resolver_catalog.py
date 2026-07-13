from typing import Final

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

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


def test_resolver_table_privileges_are_an_exact_select_allowlist() -> None:
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

    assert table_grants == {(table, "SELECT") for table in RESOLVER_SELECT_TABLES}
    assert column_grants == []


def test_resolver_functions_have_exact_hardened_catalog_posture() -> None:
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
