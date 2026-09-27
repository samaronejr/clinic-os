"""Account deactivation must close the resolver-owned tenant entry boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.tenancy.models import TenantProbe
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)
CATALOG_QUERY = """
    SELECT pg_get_userbyid(proowner), proacl::text, prosecdef,
           provolatile, proparallel, proconfig
    FROM pg_proc WHERE oid='clinic_app.user_has_org(uuid)'::regprocedure
"""


def test_deactivated_member_cannot_enter_tenant_context(
    tenant_graph: TenantGraph,
) -> None:
    graph = tenant_graph
    with runtime_role(), tenant_context(graph.user_a, graph.organization_a):
        assert TenantProbe.objects.count() == 1
    User.objects.filter(pk=graph.user_a).update(is_active=False)
    with runtime_role():
        with (
            pytest.raises(TenantAccessDeniedError),
            tenant_context(graph.user_a, graph.organization_a),
        ):
            pass
        # A service with no additional permission check cannot inherit a tenant.
        assert TenantProbe.objects.count() == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT NULLIF(current_setting('app.current_user_id', true), ''), "
                "NULLIF(current_setting('app.current_tenant', true), '')"
            )
            assert cursor.fetchone() == (None, None)
    User.objects.filter(pk=graph.user_a).update(is_active=True)
    with runtime_role(), tenant_context(graph.user_a, graph.organization_a):
        assert TenantProbe.objects.count() == 1


def test_resolver_rechecks_deactivation_on_the_same_runtime_connection(
    tenant_graph: TenantGraph,
    app_database_url: str,
) -> None:
    graph = tenant_graph
    with psycopg.connect(app_database_url, autocommit=True) as runtime:
        runtime.execute(
            "SELECT set_config('app.current_user_id', %s, false)", [str(graph.user_a)]
        )
        assert runtime.execute(
            "SELECT clinic_app.user_has_org(%s)", [graph.organization_a]
        ).fetchone() == (True,)
        User.objects.filter(pk=graph.user_a).update(is_active=False)
        assert runtime.execute(
            "SELECT clinic_app.user_has_org(%s)", [graph.organization_a]
        ).fetchone() == (False,)
        User.objects.filter(pk=graph.user_a).update(is_active=True)
        assert runtime.execute(
            "SELECT clinic_app.user_has_org(%s)", [graph.organization_a]
        ).fetchone() == (True,)
        assert runtime.execute(
            "SELECT clinic_app.user_has_org(%s)", [graph.organization_b]
        ).fetchone() == (False,)


def test_active_actor_migration_reverses_without_changing_membership_or_posture(
    tenant_graph: TenantGraph,
    app_database_url: str,
) -> None:
    graph = tenant_graph
    User.objects.filter(pk=graph.user_a).update(is_active=False)
    user_before = User.objects.filter(pk=graph.user_a).values().get()
    with owner_context(graph.organization_a):
        memberships = list(UserClinicRole.objects.filter(user_id=graph.user_a).values())
        assert memberships
    with connection.cursor() as cursor:
        cursor.execute(CATALOG_QUERY)
        posture = cursor.fetchone()
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("tenancy", "0004_protected_fields")])
        with psycopg.connect(app_database_url) as runtime:
            runtime.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                [str(graph.user_a)],
            )
            assert runtime.execute(
                "SELECT clinic_app.user_has_org(%s)", [graph.organization_a]
            ).fetchone() == (True,)
        MigrationExecutor(connection).migrate(targets)
        with psycopg.connect(app_database_url) as runtime:
            runtime.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                [str(graph.user_a)],
            )
            assert runtime.execute(
                "SELECT clinic_app.user_has_org(%s)", [graph.organization_a]
            ).fetchone() == (False,)
        with connection.cursor() as cursor:
            cursor.execute(CATALOG_QUERY)
            assert cursor.fetchone() == posture
        assert User.objects.filter(pk=graph.user_a).values().get() == user_before
        with owner_context(graph.organization_a):
            assert (
                list(UserClinicRole.objects.filter(user_id=graph.user_a).values())
                == memberships
            )
    finally:
        MigrationExecutor(connection).migrate(targets)
