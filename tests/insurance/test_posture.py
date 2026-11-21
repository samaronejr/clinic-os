from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.insurance import services
from apps.insurance.errors import InsuranceDeniedError
from django.db import connection
from psycopg import sql

from billing.journal_support import finance_scope
from identity.permission_support import owner_context, permission_context
from infra.insurance_catalog import TABLES
from infra.populated_probe import (
    Runtime,
    Witness,
    Writes,
    assert_read,
    assert_writes,
    runtime_connection,
)
from insurance.cycle_support import reconcile_cycle

if TYPE_CHECKING:
    from insurance.cycle_support import Cycle

pytestmark = pytest.mark.django_db(transaction=True)


def test_expiry_has_no_plaintext_storage_shadow() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name,column_name,data_type "
            "FROM information_schema.columns WHERE table_schema='clinic_app' "
            "AND ((table_name='intake_insurancemembership' "
            "AND column_name='valid_until') OR "
            "(table_name='insurance_insurancemembership' "
            "AND column_name='valid_to'))"
        )
        columns = cursor.fetchall()
    assert (
        "intake_insurancemembership",
        "valid_until",
        "bytea",
    ) in columns
    assert all(kind == "bytea" for _, _, kind in columns)


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
    reconcile_cycle(cycle)
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


def test_payment_match_is_inaccessible_to_another_tenant(
    cycle: Cycle, app_database_url: str
) -> None:
    match = reconcile_cycle(cycle)
    graph = cycle.finance.graph
    with finance_scope(cycle.finance), connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_jsonb(p)::text FROM clinic_app.insurance_paymentmatch p "
            "WHERE p.id=%s AND p.organization_id=%s",
            [match.pk, graph.organization_a],
        )
        source = cursor.fetchone()
    assert source is not None
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user_id=graph.clinic_admin,
            role=UserClinicRole.Role.FINANCE,
        )
    with runtime_connection(
        app_database_url, Runtime(graph.clinic_admin, graph.organization_b)
    ) as db:
        row = Witness("id", match.pk)
        assert_read(db, "insurance_paymentmatch", row, "0")
        assert_writes(
            db,
            "insurance_paymentmatch",
            row,
            Writes("id", insert="42501: insurance access denied"),
            payload=source[0],
        )


def test_resubmission_of_foreign_and_missing_guides_is_denied_alike(
    cycle: Cycle,
) -> None:
    graph = cycle.finance.graph
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user_id=graph.clinic_admin,
            role=UserClinicRole.Role.FINANCE,
        )
    denials: list[tuple[object, ...]] = []
    with permission_context(
        replace(graph, organization_a=graph.organization_b), graph.clinic_admin
    ):
        for guide_id in (cycle.guide.pk, uuid4()):
            with pytest.raises(InsuranceDeniedError) as denied:
                services.resubmit_guide(
                    clinic_id=graph.clinic_c, guide_id=guide_id, command_id=uuid4()
                )
            denials.append(denied.value.args)
    assert denials[0] == denials[1]


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
