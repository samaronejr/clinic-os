from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from test_intake_migrations import _default_connection, _scratch_database

type FunctionFact = tuple[
    str,
    str,
    bool,
    list[str] | None,
    bool,
    bool,
    bool,
]


def _append_function_catalog() -> list[FunctionFact]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT procedure.proname,
                   pg_get_userbyid(procedure.proowner),
                   procedure.prosecdef,
                   procedure.proconfig,
                   has_function_privilege(
                       'clinic_app', procedure.oid, 'EXECUTE'
                   ),
                   has_function_privilege(
                       'clinic_resolver', procedure.oid, 'EXECUTE'
                   ),
                   has_function_privilege('public', procedure.oid, 'EXECUTE')
            FROM pg_proc AS procedure
            WHERE procedure.pronamespace = 'clinic_app'::regnamespace
              AND procedure.proname IN (
                  'audit_append',
                  'audit_append_system',
                  'audit_append_unchecked_v1',
                  'audit_append_system_unchecked_v1'
              )
            ORDER BY procedure.proname
            """
        )
        return [
            (
                str(row[0]),
                str(row[1]),
                bool(row[2]),
                None if row[3] is None else [str(setting) for setting in row[3]],
                bool(row[4]),
                bool(row[5]),
                bool(row[6]),
            )
            for row in cursor.fetchall()
        ]


LATEST_FUNCTIONS: list[FunctionFact] = [
    (
        "audit_append",
        "clinic_owner",
        True,
        ["search_path=pg_catalog, pg_temp"],
        True,
        False,
        False,
    ),
    (
        "audit_append_system",
        "clinic_owner",
        True,
        ["search_path=pg_catalog, pg_temp"],
        False,
        False,
        False,
    ),
    (
        "audit_append_system_unchecked_v1",
        "clinic_owner",
        True,
        ["search_path=pg_catalog, pg_temp"],
        False,
        False,
        False,
    ),
    (
        "audit_append_unchecked_v1",
        "clinic_owner",
        True,
        ["search_path=pg_catalog, pg_temp"],
        False,
        False,
        False,
    ),
]

REVERSED_FUNCTIONS: list[FunctionFact] = [
    (
        "audit_append",
        "clinic_owner",
        True,
        ["search_path=pg_catalog, pg_temp"],
        True,
        False,
        False,
    ),
    (
        "audit_append_system",
        "clinic_owner",
        True,
        ["search_path=pg_catalog, pg_temp"],
        False,
        False,
        False,
    ),
]


AUDIT_V2 = ("audit", "0005_clinic_metadata_v2")
AUDIT_V1_BOUNDARY = ("audit", "0003_immutability_and_verification")


@pytest.mark.django_db(transaction=True)
def test_boundary_migration_catalog_and_reverse_are_exact(
    superuser_database_url: str,
) -> None:
    # The protected-field migrations are irreversible, so the reverse leg
    # runs on a scratch database migrated forward to the v2 boundary: the
    # unapply plan then contains only the reversible audit migrations.
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        MigrationExecutor(connection).migrate([AUDIT_V2])
        assert _append_function_catalog() == LATEST_FUNCTIONS

        MigrationExecutor(connection).migrate([AUDIT_V1_BOUNDARY])
        assert _append_function_catalog() == REVERSED_FUNCTIONS

        MigrationExecutor(connection).migrate([AUDIT_V2])
        assert _append_function_catalog() == LATEST_FUNCTIONS
