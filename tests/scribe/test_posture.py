"""Pin the deployed capture policies independently of migration SQL."""

from typing import Final

import pytest
from apps.scribe.rls import CUSTOM_RLS_TABLES
from django.apps import apps
from django.db import connection, transaction

pytestmark = pytest.mark.django_db(transaction=True)

TABLES: Final = frozenset(
    {
        "scribe_recordingsession",
        "scribe_captureepoch",
        "scribe_audiochunk",
        "scribe_recordingcommand",
    }
)
TENANT: Final = (
    "(organization_id = (NULLIF(current_setting("
    "'app.current_tenant'::text, true), ''::text))::uuid)"
)
PHYSICIAN: Final = (
    "(physician_id = (NULLIF(current_setting("
    "'app.current_user_id'::text, true), ''::text))::uuid)"
)


def test_every_scribe_table_has_exact_enable_force_and_owner_posture() -> None:
    # Given: every concrete capture table, including the append-only command log.
    model_tables = {
        model._meta.db_table for model in apps.get_app_config("scribe").get_models()
    }
    assert model_tables == CUSTOM_RLS_TABLES == TABLES

    # When: the deployed catalog, not the declaration, supplies the flags.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
            "c.relowner::regrole::text "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'clinic_app' AND c.relname = ANY(%s)",
            [sorted(TABLES)],
        )
        observed = set(cursor.fetchall())

    # Then: no table may be missing, unforced, or owned by a runtime role.
    assert observed == {(table, True, True, "clinic_owner") for table in TABLES}


def test_scribe_policy_roles_commands_and_predicates_are_exact() -> None:
    # Given: independent expected predicates and a stable deparsing namespace.
    expected = set()
    for table in TABLES:
        reference = "id" if table == "scribe_recordingsession" else "session_id"
        assigned = f"clinic_app.scribe_assigned({reference})"
        insertion = (
            f"({TENANT} AND {PHYSICIAN} AND clinic_app.ehr_assigned(encounter_id))"
            if table == "scribe_recordingsession"
            else f"({TENANT} AND {assigned})"
        )
        expected.update(
            {
                (
                    table,
                    "setup_tenant",
                    "PERMISSIVE",
                    ("clinic_owner",),
                    "ALL",
                    TENANT,
                    TENANT,
                ),
                (
                    table,
                    "capture_read",
                    "PERMISSIVE",
                    ("clinic_app",),
                    "SELECT",
                    assigned,
                    None,
                ),
                (
                    table,
                    "capture_insert",
                    "PERMISSIVE",
                    ("clinic_app",),
                    "INSERT",
                    None,
                    insertion,
                ),
            }
        )
        if table != "scribe_recordingcommand":
            expected.add(
                (
                    table,
                    "capture_update",
                    "PERMISSIVE",
                    ("clinic_app",),
                    "UPDATE",
                    assigned,
                    assigned,
                )
            )

    # When: every policy is read, including unexpected permissive additions.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL search_path = pg_catalog")
        cursor.execute(
            "SELECT tablename, policyname, permissive, roles, cmd, qual, with_check "
            "FROM pg_catalog.pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)",
            [sorted(TABLES)],
        )
        observed = {
            (table, name, mode, tuple(roles), command, using, check)
            for table, name, mode, roles, command, using, check in cursor.fetchall()
        }

    # Then: role, command, ownership, and both predicates stay exact.
    assert observed == expected
