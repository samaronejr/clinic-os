"""Unused analytics schema reverses; retained metric history requires restore."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.analytics.models import ReadModelRefresh
from apps.analytics.services import refresh_metric
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import WorkflowDefinitionVersion
from apps.workflows.services import publish_definition
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor

from analytics.test_metrics import metric_query
from billing.journal_support import finance_scope, finance_setup
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from billing.journal_support import FinanceSetup
    from rbac_fixtures import RbacGraph

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)


def _functions() -> list[tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid=p.pronamespace"
            " WHERE n.nspname='clinic_app'"
            " AND (p.proname LIKE 'analytics_%'"
            " OR p.proname='workflows_steps_valid') ORDER BY p.proname"
        )
        return [(str(name), str(body)) for name, body in cursor.fetchall()]


def test_unused_analytics_reverses_and_reapplies_with_original_grants() -> None:
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    before = _functions()
    try:
        executor.migrate([("analytics", None)])
        assert [name for name, _body in _functions()] == ["workflows_steps_valid"]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('clinic_app.analytics_readmodelrefresh'),"
                " has_column_privilege('clinic_resolver',"
                " 'clinic_app.audit_event_tenant','payload','SELECT'),"
                " has_table_privilege('clinic_resolver',"
                " 'clinic_app.scribe_reviewdecision','SELECT'),"
                " has_table_privilege('clinic_resolver',"
                " 'clinic_app.scribe_draftsectionsuggestion','SELECT'),"
                " has_table_privilege('clinic_resolver',"
                " 'clinic_app.scribe_aidraft','SELECT')"
            )
            assert cursor.fetchone() == (None, False, True, True, True)
    finally:
        MigrationExecutor(connection).migrate(targets)
    assert _functions() == before


def test_retained_metric_history_refuses_schema_removal(
    finance_setup: FinanceSetup,
) -> None:
    setup = finance_setup
    with finance_scope(setup):
        result = refresh_metric(
            clinic_id=setup.clinic_id,
            query=metric_query("no_show"),
            command_id=uuid4(),
        )
    executor = MigrationExecutor(connection)
    target = ("analytics", "0001_initial")
    migration = executor.loader.get_migration(*target)
    before = _functions()
    with (
        pytest.raises(DatabaseError, match="metric history requires restore"),
        connection.schema_editor() as editor,
    ):
        migration.unapply(executor.loader.project_state([target]), editor)
    assert _functions() == before
    with finance_scope(setup):
        assert ReadModelRefresh.objects.get(pk=result.refresh_id).descriptor


def test_retained_metric_workflow_refuses_removing_its_handler(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="clinic_admin",
            workflow_bundle_version=2,
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        definition = publish_definition(
            clinic_id=graph.clinic_a,
            key="retained-metrics",
            steps=[{"handler": "metrics_refresh", "metric_id": "no_show", "days": 30}],
            idempotency_key=uuid4(),
        )
    executor = MigrationExecutor(connection)
    target = ("workflows", "0009_metrics_refresh")
    migration = executor.loader.get_migration(*target)
    before = _functions()
    with (
        pytest.raises(DatabaseError, match="metric workflows require restore"),
        connection.schema_editor() as editor,
    ):
        migration.unapply(executor.loader.project_state([target]), editor)
    assert _functions() == before
    with owner_context(graph.organization_a):
        assert WorkflowDefinitionVersion.objects.get(pk=definition.pk).steps == [
            {"handler": "metrics_refresh", "metric_id": "no_show", "days": 30}
        ]
