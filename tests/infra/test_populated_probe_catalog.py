"""Live catalog completeness and intentional cross-tenant catalog visibility."""

from __future__ import annotations

import pytest
from apps.subscriptions.models import Entitlement, Plan
from apps.tenancy.models import TenantScopedModel
from django.apps import apps
from django.db import connection

from infra.populated_probe import (
    Witness,
    assert_read,
    assert_writes,
    foreign_staff,
    runtime_connection,
)
from infra.populated_probe_inventory import inventory

pytestmark = pytest.mark.django_db(transaction=True)


def test_live_tenant_and_policy_relations_must_be_mapped() -> None:
    rows = inventory()
    tables = {row.relation for row in rows}
    assert len(rows) == len(tables), "duplicate relation in populated probe map"
    required = {
        model._meta.db_table
        for model in apps.get_models()
        if issubclass(model, TenantScopedModel)
        and not model._meta.proxy
        and not model._meta.abstract
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_catalog.pg_policies WHERE schemaname = %s",
            ["clinic_app"],
        )
        required.update(row[0] for row in cursor.fetchall())
    # Unchanged SQL-only discarded drafts predate this wave; that relation has
    # its own posture test and is not a model.
    required.remove("ehr_discarded_content")
    assert tables == required


def test_operation_map_matches_effective_runtime_privileges() -> None:
    # The same row policy applies no matter which column an UPDATE targets.
    # This complementary check catches a column-only grant on any column,
    # including one that the concrete witness statement doesn't use.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, c.relrowsecurity AND c.relforcerowsecurity,
                has_any_column_privilege('clinic_app', c.oid, 'SELECT'),
                has_any_column_privilege('clinic_app', c.oid, 'INSERT'),
                has_any_column_privilege('clinic_app', c.oid, 'UPDATE'),
                has_table_privilege('clinic_app', c.oid, 'DELETE'),
                has_table_privilege('clinic_app', c.oid, 'TRUNCATE')
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'clinic_app' AND c.relkind IN ('r', 'p')
            """
        )
        live = {row[0]: tuple(row[1:]) for row in cursor.fetchall()}
    for probe in inventory():
        assert probe.select in ({"1"} if probe.scope == "shared" else {"0", "ACL"})
        assert probe.update in {"0", "ACL"}
        assert probe.delete in {"0", "ACL"}
        assert probe.truncate == "ACL"
        assert all(
            code in {"ACL", "WITH CHECK"} or code.startswith(("42501: ", "23514: "))
            for code in probe.insert.split(" | ")
        ), probe.relation
        privileges = tuple(
            outcome != "ACL"
            for outcome in (
                probe.select,
                probe.insert,
                probe.update,
                probe.delete,
                probe.truncate,
            )
        )
        assert live[probe.relation] == (True, *privileges), probe.relation


class TestSharedCatalogs:
    """Packaging versions and quotas are intentionally global, not tenant data."""

    @pytest.mark.parametrize(
        "table", ["subscriptions_plan", "subscriptions_entitlement"]
    )
    def test_clinic_app_can_read_but_cannot_write(
        self, app_database_url: str, table: str
    ) -> None:
        (probe,) = [row for row in inventory() if row.relation == table]
        assert probe.scope == "shared"
        plan = Plan.objects.create(code="core_operations", version=9001)
        allowance = Entitlement.objects.create(plan=plan, meter="seats", quota=1)
        row = Witness("id", plan.pk if table == "subscriptions_plan" else allowance.pk)
        for authority in (foreign_staff(), foreign_staff()):
            with runtime_connection(app_database_url, authority) as db:
                assert_read(db, table, row, probe.select)
                assert_writes(db, table, row, probe.writes)
