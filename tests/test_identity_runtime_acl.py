from __future__ import annotations

from typing import Final

import psycopg
import pytest
from django.db import connection
from psycopg.errors import InsufficientPrivilege

pytestmark = pytest.mark.django_db(transaction=True)

SELECT_ONLY_TABLES: Final = {
    "django_migrations",
    "identity_clinic",
    "identity_organization",
    "identity_userclinicrole",
}


def test_runtime_identity_and_migration_table_acls_are_exact(
    app_database_url: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app'
              AND table_schema = 'clinic_app'
              AND table_name = ANY(%s)
            """,
            [list(SELECT_ONLY_TABLES)],
        )
        grants = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'identity_user'
            UNION ALL
            SELECT privilege_type
            FROM information_schema.role_column_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'identity_user'
            """
        )
        user_grants = cursor.fetchall()
    assert grants == {(table, "SELECT") for table in SELECT_ONLY_TABLES}
    assert user_grants == []

    statements = [
        "UPDATE clinic_app.identity_organization SET name = name WHERE false",
        "UPDATE clinic_app.identity_clinic SET name = name WHERE false",
        "DELETE FROM clinic_app.identity_userclinicrole WHERE false",
        "DELETE FROM clinic_app.django_migrations WHERE false",
        "SELECT id FROM clinic_app.identity_user LIMIT 1",
    ]
    with psycopg.connect(app_database_url) as app_connection:
        assert (
            app_connection.execute(
                "SELECT count(*) FROM clinic_app.django_migrations"
            ).fetchone()
            is not None
        )
        for statement in statements:
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute(statement)
            app_connection.rollback()


def test_unique_role_and_physician_resolver_catalog_posture_are_exact() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT constraint_row.conname,
                   ARRAY(
                       SELECT attribute.attname
                       FROM unnest(constraint_row.conkey)
                            WITH ORDINALITY AS key(attnum, position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.conrelid
                        AND attribute.attnum = key.attnum
                       ORDER BY key.position
                   )
            FROM pg_constraint AS constraint_row
            WHERE constraint_row.conrelid =
                  'clinic_app.identity_userclinicrole'::regclass
              AND constraint_row.contype = 'u'
              AND constraint_row.conname = 'identity_userclinicrole_assignment_uniq'
            """
        )
        constraint = cursor.fetchone()
        cursor.execute(
            """
            SELECT procedure.proowner::regrole::text,
                   procedure.prosecdef,
                   procedure.provolatile,
                   procedure.proparallel,
                   procedure.proconfig,
                   pg_catalog.has_function_privilege(
                       'clinic_app', procedure.oid, 'EXECUTE'
                   ),
                   pg_catalog.has_function_privilege(
                       'public', procedure.oid, 'EXECUTE'
                   ),
                   pg_catalog.pg_get_function_result(procedure.oid)
            FROM pg_catalog.pg_proc AS procedure
            WHERE procedure.oid = to_regprocedure(
                'clinic_app.list_active_clinic_physicians(uuid)'
            )
            """
        )
        resolver = cursor.fetchone()
        cursor.execute(
            "SELECT to_regprocedure(%s)",
            ["clinic_app._phase1a_identity_canonicalization_candidates()"],
        )
        migration_helper = cursor.fetchone()

    assert constraint == (
        "identity_userclinicrole_assignment_uniq",
        ["organization_id", "clinic_id", "user_id", "role"],
    )
    assert resolver == (
        "clinic_resolver",
        True,
        "s",
        "u",
        ["search_path=pg_catalog, clinic_app, pg_temp"],
        True,
        False,
        "TABLE(user_id uuid, display_label text)",
    )
    assert migration_helper == (None,)
