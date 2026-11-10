"""Real PostgreSQL denies cross-clinic reads and direct money-history mutation."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.billing.collections import (
    observe_settlement,
    post_settlement,
    verify_settlement,
)
from apps.billing.journal import Line, SourceFact, post_entry
from apps.billing.models import JournalBook, JournalEntry
from apps.billing.money_types import SettlementObservation
from apps.billing.services import BillingAccessDeniedError
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from psycopg import Error as PostgresError
from psycopg import connect, sql

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from identity.permission_support import owner_context
from infra.journal_catalog import TABLES
from patient_service_support import runtime_role

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_every_journal_table_has_exact_forced_rls_and_no_delete_or_truncate_grant() -> (
    None
):
    # Given: the independently enumerated operational tables.
    with connection.cursor() as cursor:
        # When: the real catalog is observed, including owner and default privileges.
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
            "c.relowner::regrole::text FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='clinic_app' AND "
            "c.relname=ANY(%s)",
            [sorted(TABLES)],
        )
        posture = set(cursor.fetchall())
        cursor.execute(
            "SELECT tablename,policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [sorted(TABLES)],
        )
        policies = set(cursor.fetchall())
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name=ANY(%s) AND privilege_type "
            "IN ('DELETE','TRUNCATE','TRIGGER','REFERENCES','UPDATE')",
            [sorted(TABLES)],
        )
        dangerous = cursor.fetchall()
    # Then: no undeclared owner/policy/grant survives bootstrap defaults.
    assert posture == {(table, True, True, "clinic_owner") for table in TABLES}
    assert policies == {(table, "billing_journal_scope") for table in TABLES}
    assert dangerous == []


def test_balance_triggers_are_actually_constraint_triggers_deferred_to_commit() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname,t.tgdeferrable,t.tginitdeferred "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND t.tgname='billing_entry_balanced'"
        )
        assert set(cursor.fetchall()) == {
            ("billing_journalentry", True, True),
            ("billing_journalline", True, True),
        }


def test_foreign_and_unknown_books_have_identical_denials(
    finance_setup: FinanceSetup,
) -> None:
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.shared_user, finance_setup.graph.organization_b
        ),
    ):
        assert not JournalBook.objects.exists()
        for book_id in (finance_setup.book_id, uuid4()):
            with pytest.raises(BillingAccessDeniedError):
                post_entry(
                    book_id=book_id,
                    source_fact=SourceFact("manual", uuid4()),
                    lines=(
                        Line("bank", "debit", 1),
                        Line("opening_balances", "credit", 1),
                    ),
                )


def test_a_clinician_role_does_not_read_the_operational_book(
    finance_setup: FinanceSetup,
) -> None:
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.physician, finance_setup.graph.organization_a
        ),
    ):
        assert not JournalBook.objects.exists()
        assert not JournalEntry.objects.exists()


def test_duplicate_delivery_of_one_verified_collection_posts_once(
    finance_setup: FinanceSetup,
) -> None:
    evidence = uuid4()
    with finance_scope(finance_setup):
        observation = SettlementObservation(1000, "patient", "bank", evidence)
        first = observe_settlement(
            clinic_id=finance_setup.clinic_id,
            observation=observation,
        )
        repeated = observe_settlement(
            clinic_id=finance_setup.clinic_id,
            observation=observation,
        )
        assert first.pk == repeated.pk
        verify_settlement(clinic_id=finance_setup.clinic_id, settlement_id=first.pk)
        post_settlement(clinic_id=finance_setup.clinic_id, settlement_id=first.pk)
        post_settlement(clinic_id=finance_setup.clinic_id, settlement_id=repeated.pk)
        assert (
            JournalEntry.objects.filter(
                source_kind="collection", source_id=first.pk
            ).count()
            == 1
        )


def test_runtime_cannot_delete_or_truncate_any_financial_history(
    finance_setup: FinanceSetup,
) -> None:
    for table in sorted(TABLES):
        for verb in ("DELETE FROM", "TRUNCATE"):
            with (
                finance_scope(finance_setup),
                pytest.raises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    sql.SQL("{} {}").format(
                        sql.SQL(verb), sql.Identifier("clinic_app", table)
                    )
                )


def test_posted_entry_and_lines_cannot_be_changed_even_by_owner(
    finance_setup: FinanceSetup,
    superuser_database_url: str,
) -> None:
    with finance_scope(finance_setup):
        entry = post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=(Line("bank", "debit", 100), Line("opening_balances", "credit", 100)),
        )
    # When/Then: neither an entry nor its child rows admits UPDATE or DELETE.
    for statement in (
        "UPDATE clinic_app.billing_journalentry SET state='proposed' WHERE id=%s",
        "UPDATE clinic_app.billing_journalline SET amount_minor=101 WHERE entry_id=%s",
        "DELETE FROM clinic_app.billing_journalline WHERE entry_id=%s",
    ):
        with owner_context(finance_setup.graph.organization_a):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_user_id',%s,true)",
                    [str(finance_setup.graph.clinic_admin)],
                )
            with (
                pytest.raises(DatabaseError) as rejected,
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement, [entry.pk])
            cause = rejected.value.__cause__
            assert isinstance(cause, PostgresError)
            assert cause.sqlstate == "42501"
            helper = (
                "billing_book_access"
                if "billing_journalentry SET" in statement
                else "billing_entry_readable"
            )
            assert cause.diag.message_primary == (
                f"permission denied for function {helper}"
            )
        # A real privileged connection bypasses RLS, but not the row trigger.
        with connect(superuser_database_url) as privileged:
            assert privileged.execute(
                "SELECT current_user,rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == ("clinic_super", True)
            assert privileged.execute(
                "SELECT bool_and(relforcerowsecurity),"
                "bool_and(has_table_privilege(current_user,oid,'UPDATE,DELETE')) "
                "FROM pg_class WHERE oid IN "
                "('clinic_app.billing_journalentry'::regclass,"
                "'clinic_app.billing_journalline'::regclass)"
            ).fetchone() == (True, True)
            privileged.execute(
                "SELECT set_config('app.current_tenant',%s,true),"
                "set_config('app.current_user_id',%s,true)",
                [
                    str(finance_setup.graph.organization_a),
                    str(finance_setup.graph.clinic_admin),
                ],
            )
            with pytest.raises(PostgresError) as trigger_rejected:
                privileged.execute(statement, [entry.pk])
            assert trigger_rejected.value.sqlstate == "23514"
