"""Exact FORCE-RLS, policy, grant and encrypted-column posture for specialty data."""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

POLICIES = {
    "ehr_specialtypack": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("pack_read", "SELECT", "clinic_app"),
        ("pack_insert", "INSERT", "clinic_app"),
        ("pack_retire", "UPDATE", "clinic_app"),
    },
    "ehr_specialtyentry": {
        ("setup_tenant", "ALL", "clinic_owner"),
        ("specialty_read", "SELECT", "clinic_app"),
        ("specialty_insert", "INSERT", "clinic_app"),
    },
}


@pytest.mark.parametrize("table", POLICIES)
def test_specialty_policy_and_privilege_sets_are_closed(table: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid=('clinic_app.'||%s)::regclass",
            [table],
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute(
            "SELECT policyname,cmd,roles FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=%s",
            [table],
        )
        rows = cursor.fetchall()
        assert {(name, cmd, roles[0]) for name, cmd, roles in rows} == POLICIES[table]
        assert all(len(roles) == 1 for _name, _cmd, roles in rows)
        cursor.execute(
            "SELECT has_table_privilege('clinic_app',"
            "('clinic_app.'||%s)::regclass,'DELETE,TRUNCATE'),"
            "has_table_privilege('clinic_agent',"
            "('clinic_app.'||%s)::regclass,'SELECT,INSERT,UPDATE,DELETE')",
            [table, table],
        )
        assert cursor.fetchone() == (False, False)
        cursor.execute(
            "SELECT tgenabled FROM pg_trigger "
            "WHERE tgrelid=('clinic_app.'||%s)::regclass "
            "AND tgname IN ('specialty_binding','specialty_no_truncate') "
            "ORDER BY tgname",
            [table],
        )
        assert cursor.fetchall() == [("O",), ("O",)]


def test_specialty_details_have_no_plaintext_shadow() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name,data_type FROM information_schema.columns "
            "WHERE table_schema='clinic_app' AND table_name='ehr_specialtyentry' "
            "AND column_name IN ('details','details_legacy')"
        )
        assert cursor.fetchall() == [("details", "bytea")]
