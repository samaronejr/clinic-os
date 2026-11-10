"""Independent real-catalog PSP posture and tenant-denial regressions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.billing.psp_models import PspIntent
from apps.billing.psp_services import prepare_psp_charge
from apps.core import integration
from apps.tenancy.db import tenant_context
from django.db import connection

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_psp_reconciliation import sandbox
from infra.psp_catalog import TABLES
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_psp_tables_are_forced_rls_append_only_and_have_exact_policies() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity,"
            "relowner::regrole::text FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='clinic_app' "
            "AND relname=ANY(%s)",
            [sorted(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT tablename,policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [sorted(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, policy)
            for table in TABLES
            for policy in ("billing_psp_read", "billing_psp_insert")
        }
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' AND grantee='clinic_app' "
            "AND table_name=ANY(%s)",
            [sorted(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, permission)
            for table in TABLES
            for permission in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT c.relname,t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='clinic_app' "
            "AND c.relname=ANY(%s) AND NOT t.tgisinternal",
            [sorted(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, trigger)
            for table in TABLES
            for trigger in ("billing_psp_guard", "billing_psp_no_truncate")
        }


def test_no_context_or_foreign_organization_sees_psp_intents(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
        assert PspIntent.objects.filter(pk=intent.pk).exists()
    with runtime_role():
        assert not PspIntent.objects.exists()
        with tenant_context(
            finance_setup.graph.shared_user, finance_setup.graph.organization_b
        ):
            assert not PspIntent.objects.exists()
