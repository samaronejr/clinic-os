from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import connection
from psycopg import sql

from billing.journal_support import finance_scope
from infra.insurance_catalog import TABLES

if TYPE_CHECKING:
    from insurance.cycle_support import Cycle

pytestmark = pytest.mark.django_db(transaction=True)


def test_every_insurance_table_has_exact_force_rls_and_least_privileges() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
            "c.relowner::regrole::text FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relname=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT tablename,policyname FROM pg_catalog.pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, "insurance_scope") for table in TABLES
        }
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' AND grantee='clinic_app' "
            "AND table_name=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, privilege) for table in TABLES for privilege in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT table_name,column_name FROM information_schema.column_privileges "
            "WHERE table_schema='clinic_app' AND grantee='clinic_app' "
            "AND privilege_type='UPDATE' AND table_name=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, "state")
            for table in ("insurance_guide", "insurance_batch", "insurance_appeal")
        }


def test_cross_organization_context_cannot_read_any_insurance_relation(
    cycle: Cycle,
) -> None:
    with finance_scope(cycle.finance), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(cycle.finance.graph.organization_b)],
        )
        for table in TABLES:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM clinic_app.{}").format(
                    sql.Identifier(table)
                )
            )
            assert cursor.fetchone() == (0,)


def test_membership_scope_foreign_keys_are_validated_and_immediate() -> None:
    expected = {
        ("intake_insurancemembership", "intake_membership_payer_org_fk"),
        ("insurance_insurancemembership", "insurance_registration_org_fk"),
        ("insurance_payerrequest", "insurance_request_appointment_scope_fk"),
        ("scheduling_appointment", "scheduling_membership_scope_fk"),
        ("scheduling_appointment", "scheduling_contract_scope_fk"),
        ("scheduling_appointment", "scheduling_eligibility_scope_fk"),
        ("scheduling_appointment", "scheduling_authorization_scope_fk"),
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.conrelid::regclass::text,c.conname,"
            "c.convalidated,c.condeferrable "
            "FROM pg_catalog.pg_constraint c "
            "WHERE c.contype='f' AND c.conname=ANY(%s)",
            [[name for _, name in expected]],
        )
        rows = cursor.fetchall()
    assert {
        (table.removeprefix("clinic_app."), name, validated, deferred)
        for table, name, validated, deferred in rows
    } == {(table, name, True, False) for table, name in expected}
