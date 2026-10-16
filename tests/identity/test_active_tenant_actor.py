"""Account deactivation must close the resolver-owned tenant entry boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.tenancy.models import TenantProbe
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from database_urls import database_url_for_name
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.test_intake_migrations import _default_connection, _scratch_database

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
    superuser_database_url: str,
) -> None:
    graph = tenant_graph
    clinic_id = uuid4()
    # Reverse only the historical resolver on an owned historical schema.
    # The current lifecycle and its irreversible authority history stay intact.
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        targets = [("tenancy", "0005_active_tenant_actor")]
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        historical = executor.loader.project_state(targets).apps
        user_model = historical.get_model("identity", "User")
        role_model = historical.get_model("identity", "UserClinicRole")
        organization_model = historical.get_model("identity", "Organization")
        clinic_model = historical.get_model("identity", "Clinic")
        user_model.objects.create(
            id=graph.user_a, username="synthetic-active-migration"
        )
        with owner_context(graph.organization_a):
            organization_model.objects.create(
                id=graph.organization_a,
                name="Synthetic historical organization",
                cnpj="00000000000001",
            )
            clinic_model.objects.create(
                id=clinic_id,
                organization_id=graph.organization_a,
                name="Synthetic historical clinic",
                crm_uf="SP",
                timezone="America/Sao_Paulo",
            )
            role_model.objects.create(
                user_id=graph.user_a,
                clinic_id=clinic_id,
                organization_id=graph.organization_a,
                role="owner",
            )
        user_model.objects.filter(pk=graph.user_a).update(is_active=False)
        user_before = user_model.objects.filter(pk=graph.user_a).values().get()
        with owner_context(graph.organization_a):
            memberships = list(role_model.objects.filter(user_id=graph.user_a).values())
            assert memberships
        with connection.cursor() as cursor:
            cursor.execute(CATALOG_QUERY)
            posture = cursor.fetchone()
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([("tenancy", "0004_protected_fields")])
            with psycopg.connect(
                database_url_for_name(app_database_url, wrapper.settings_dict["NAME"])
            ) as runtime:
                runtime.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    [str(graph.user_a)],
                )
                assert runtime.execute(
                    "SELECT clinic_app.user_has_org(%s)", [graph.organization_a]
                ).fetchone() == (True,)
            MigrationExecutor(connection).migrate(targets)
            with psycopg.connect(
                database_url_for_name(app_database_url, wrapper.settings_dict["NAME"])
            ) as runtime:
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
            assert (
                user_model.objects.filter(pk=graph.user_a).values().get() == user_before
            )
            with owner_context(graph.organization_a):
                assert (
                    list(role_model.objects.filter(user_id=graph.user_a).values())
                    == memberships
                )
        finally:
            MigrationExecutor(connection).migrate(targets)
