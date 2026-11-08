"""Independent model/ACL expectations for the additive fiscal tables."""

from __future__ import annotations

from typing import Final

import pytest
from apps.billing import rls
from apps.billing.models import FiscalConfiguration, FiscalDocument, FiscalTransition
from apps.tenancy.fields import EncryptedBytesField, EncryptedJSONField
from apps.tenancy.models import TenantScopedModel
from django.apps import apps
from django.db import connection
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState

TABLES: Final = {
    "billing_fiscalconfiguration",
    "billing_fiscaldocument",
    "billing_fiscaltransition",
}
UPDATABLE: Final = {
    "status",
    "last_command_id",
    "xml_payload",
    "pdf_payload",
    "cancellation_payload",
    "xml_reference",
    "pdf_reference",
    "provider_reference",
    "rejection_code",
}


def test_fiscal_models_are_tenant_scoped_and_keep_fiscal_payloads_encrypted() -> None:
    assert {
        model._meta.db_table
        for model in (FiscalConfiguration, FiscalDocument, FiscalTransition)
        if issubclass(model, TenantScopedModel)
    } == TABLES
    assert isinstance(
        FiscalConfiguration._meta.get_field("legal_entity"), EncryptedJSONField
    )
    assert isinstance(
        FiscalConfiguration._meta.get_field("tax_fields"), EncryptedJSONField
    )
    assert isinstance(FiscalDocument._meta.get_field("tax_fields"), EncryptedJSONField)
    for field in ("request_xml", "xml_payload", "pdf_payload", "cancellation_payload"):
        assert isinstance(FiscalDocument._meta.get_field(field), EncryptedBytesField)
    assert FiscalDocument._meta.get_field("provider_reference").null
    assert set(FiscalDocument.Status.values) == {
        "draft",
        "submitted",
        "authorized",
        "rejected",
        "cancelled",
    }


def test_fiscal_registry_has_no_runtime_review_or_history_writes() -> None:
    assert TABLES <= rls.CUSTOM_RLS_TABLES
    assert {table: rls.RUNTIME_GRANTS[table] for table in TABLES} == {
        "billing_fiscalconfiguration": frozenset({"SELECT"}),
        "billing_fiscaldocument": frozenset({"SELECT", "INSERT"}),
        "billing_fiscaltransition": frozenset({"SELECT"}),
    }
    assert {
        column
        for table, column, permission in rls.COLUMN_GRANTS
        if table in TABLES and permission == "UPDATE"
    } == UPDATABLE


def test_fiscal_migration_state_matches_models_without_opening_a_database() -> None:
    loader = MigrationLoader(None)
    changes = MigrationAutodetector(
        loader.project_state(),
        ProjectState.from_apps(apps),
        NonInteractiveMigrationQuestioner(),
    ).changes(graph=loader.graph)
    assert not changes.get("billing")
    migration = loader.get_migration("billing", "0013_fiscal_documents")
    assert migration.atomic is False
    assert len(migration.operations) == 1
    assert migration.operations[0].atomic is True
    assert migration.operations[0].reversible is False


@pytest.mark.django_db(transaction=True)
def test_fiscal_tables_have_exact_force_rls_and_runtime_acls() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relname=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {(table, True, True) for table in TABLES}
        cursor.execute(
            "SELECT table_name,privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema='clinic_app' AND grantee='clinic_app' "
            "AND table_name=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            *((table, "SELECT") for table in TABLES),
            ("billing_fiscaldocument", "INSERT"),
        }
        cursor.execute(
            "SELECT table_name,column_name,privilege_type "
            "FROM information_schema.column_privileges "
            "WHERE table_schema='clinic_app' AND grantee='clinic_app' "
            "AND privilege_type='UPDATE' AND table_name=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            ("billing_fiscaldocument", column, "UPDATE") for column in UPDATABLE
        }
        cursor.execute(
            "SELECT tablename,policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            *((table, "fiscal_read") for table in TABLES),
            ("billing_fiscalconfiguration", "fiscal_review_owner"),
            ("billing_fiscaldocument", "fiscal_insert"),
            ("billing_fiscaldocument", "fiscal_update"),
        }


@pytest.mark.django_db(transaction=True)
def test_fiscal_foreign_keys_are_validated_without_changing_shared_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.conrelid::regclass::text,c.confrelid::regclass::text,"
            "c.convalidated,c.condeferrable,c.condeferred "
            "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "WHERE n.nspname='clinic_app' AND t.relname=ANY(%s) AND c.contype='f'",
            [sorted(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (f"billing_{table}", target, True, True, True)
            for table, targets in (
                (
                    "fiscalconfiguration",
                    ("billing_journalbook", "identity_clinic", "identity_organization"),
                ),
                (
                    "fiscaldocument",
                    (
                        "billing_journalbook",
                        "identity_clinic",
                        "identity_organization",
                        "billing_fiscalconfiguration",
                        "billing_invoice",
                    ),
                ),
                (
                    "fiscaltransition",
                    ("billing_fiscaldocument", "identity_organization"),
                ),
            )
            for target in targets
        }
