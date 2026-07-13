from typing import Final

import psycopg
import pytest
from django.db import connection, transaction
from psycopg.errors import InsufficientPrivilege, InvalidTextRepresentation

from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)

COUNT_PROBES_SQL: Final = "SELECT count(*) FROM clinic_app.tenancy_tenantprobe"


def _set_local(connection_: psycopg.Connection, setting: str, value: str) -> None:
    connection_.execute(
        "SELECT pg_catalog.set_config(%s, %s, true)",
        (setting, value),
    )


def test_rls_filters_cross_tenant_selects_and_updates(
    app_database_url: str,
    tenant_graph: TenantGraph,
) -> None:
    # Given: two committed probes and a runtime transaction scoped to tenant A
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(
            app_connection,
            "app.current_tenant",
            str(tenant_graph.organization_a),
        )

        # When: runtime SQL reads all probes and updates tenant B directly
        visible_labels = app_connection.execute(
            "SELECT label FROM clinic_app.tenancy_tenantprobe ORDER BY label"
        ).fetchall()
        updated_rows = app_connection.execute(
            """
            UPDATE clinic_app.tenancy_tenantprobe
            SET label = 'cross-tenant-update'
            WHERE organization_id = %s
            """,
            (tenant_graph.organization_b,),
        ).rowcount

        # Then: PostgreSQL exposes and mutates only tenant A
        assert visible_labels == [("probe-a",)]
        assert updated_rows == 0


def test_rls_with_check_rejects_cross_tenant_insert(
    app_database_url: str,
    tenant_graph: TenantGraph,
) -> None:
    # Given: a runtime transaction scoped to tenant A
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(
            app_connection,
            "app.current_tenant",
            str(tenant_graph.organization_a),
        )

        # When: runtime SQL tries to insert a tenant-B probe
        with pytest.raises(InsufficientPrivilege):
            app_connection.execute(
                """
                INSERT INTO clinic_app.tenancy_tenantprobe
                    (organization_id, label)
                VALUES (%s, 'cross-tenant-insert')
                """,
                (tenant_graph.organization_b,),
            )
        app_connection.rollback()

    # Then: the RLS WITH CHECK rejected the write


@pytest.mark.parametrize("tenant_setting", [None, ""], ids=["unset", "empty"])
def test_rls_fails_closed_without_a_tenant(
    app_database_url: str,
    tenant_graph: TenantGraph,
    tenant_setting: str | None,
) -> None:
    # Given: committed tenant rows and no usable tenant GUC
    with psycopg.connect(app_database_url) as app_connection:
        if tenant_setting is not None:
            _set_local(app_connection, "app.current_tenant", tenant_setting)

        # When: the runtime role reads the tenant table
        visible_count = app_connection.execute(COUNT_PROBES_SQL).fetchone()

        # Then: no row leaks and no error is raised
        assert visible_count == (0,)


def test_rls_malformed_tenant_aborts_closed(
    app_database_url: str,
    tenant_graph: TenantGraph,
) -> None:
    # Given: a nonempty malformed tenant GUC
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(app_connection, "app.current_tenant", "not-a-uuid")

        # When/Then: evaluating the policy raises instead of leaking rows
        with pytest.raises(InvalidTextRepresentation):
            app_connection.execute(COUNT_PROBES_SQL)
        app_connection.rollback()


def test_membership_resolvers_bind_identity_to_the_user_guc(
    app_database_url: str,
    tenant_graph: TenantGraph,
) -> None:
    # Given: a runtime connection with user A in the user GUC
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(app_connection, "app.current_user_id", str(tenant_graph.user_a))

        # When: membership is resolved, then only the GUC changes to user B
        user_a_answer = app_connection.execute(
            "SELECT clinic_app.user_has_org(%s)",
            (tenant_graph.organization_a,),
        ).fetchone()
        user_a_organizations = app_connection.execute(
            "SELECT * FROM clinic_app.user_organizations()"
        ).fetchall()
        _set_local(app_connection, "app.current_user_id", str(tenant_graph.user_b))
        user_b_answer = app_connection.execute(
            "SELECT clinic_app.user_has_org(%s)",
            (tenant_graph.organization_a,),
        ).fetchone()

        # Then: resolver results follow the trusted GUC identity only
        assert user_a_answer == (True,)
        assert user_a_organizations == [(tenant_graph.organization_a,)]
        assert user_b_answer == (False,)


def test_auth_resolvers_work_while_raw_user_access_is_denied(
    app_database_url: str,
    tenant_graph: TenantGraph,
) -> None:
    # Given: a runtime connection scoped to user A
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(app_connection, "app.current_user_id", str(tenant_graph.user_a))

        # When: definer auth functions and a raw table read are attempted
        auth_row = app_connection.execute(
            "SELECT * FROM clinic_app.auth_lookup(%s)",
            (tenant_graph.username_a,),
        ).fetchone()
        loaded_user = app_connection.execute(
            "SELECT id, username FROM clinic_app.load_current_user()"
        ).fetchone()
        with pytest.raises(InsufficientPrivilege):
            app_connection.execute("SELECT id FROM clinic_app.identity_user")
        app_connection.rollback()

        # Then: only the hardened functions expose the intended user data
        assert auth_row == (
            tenant_graph.user_a,
            tenant_graph.username_a,
            "synthetic-hash-a",
            True,
        )
        assert loaded_user == (tenant_graph.user_a, tenant_graph.username_a)


def test_user_bound_resolvers_fail_closed_without_a_user(
    app_database_url: str,
    tenant_graph: TenantGraph,
) -> None:
    # Given: a fresh runtime connection with no user GUC
    with psycopg.connect(app_database_url) as app_connection:
        # When: every user-bound resolver is called
        membership = app_connection.execute(
            "SELECT clinic_app.user_has_org(%s)",
            (tenant_graph.organization_a,),
        ).fetchone()
        organizations = app_connection.execute(
            "SELECT * FROM clinic_app.user_organizations()"
        ).fetchall()
        users = app_connection.execute(
            "SELECT id FROM clinic_app.load_current_user()"
        ).fetchall()

        # Then: each resolver returns a closed result without error
        assert membership == (False,)
        assert organizations == []
        assert users == []


def test_temporary_permissive_policy_proves_isolation_and_drift_teeth(
    app_database_url: str,
    tenant_graph: TenantGraph,
) -> None:
    # Given: a rollback-only owner transaction with a dangerous true policy
    with transaction.atomic(), connection.cursor() as owner_cursor:
        owner_cursor.execute(
            """
            CREATE POLICY todo6_teeth_allow_all
            ON clinic_app.tenancy_tenantprobe AS PERMISSIVE FOR ALL
            USING (true) WITH CHECK (true)
            """
        )
        try:
            owner_cursor.execute("SET LOCAL ROLE clinic_app")
            owner_cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(tenant_graph.organization_a)],
            )

            # When: the weakened policy and catalog drift are observed
            owner_cursor.execute(COUNT_PROBES_SQL)
            teeth_visible = owner_cursor.fetchone()
            owner_cursor.execute("RESET ROLE")
            owner_cursor.execute(
                """
                SELECT count(*) FROM pg_catalog.pg_policies
                WHERE schemaname = 'clinic_app'
                  AND policyname <> 'tenant_isolation'
                """
            )
            unexpected_policy_count = owner_cursor.fetchone()

            # Then: the dangerous policy both leaks and is detected
            assert teeth_visible == (2,)
            assert unexpected_policy_count == (1,)
        finally:
            owner_cursor.execute("RESET ROLE")
            transaction.set_rollback(True)

    # Then: rollback restores both isolation and the exact policy set
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(
            app_connection,
            "app.current_tenant",
            str(tenant_graph.organization_a),
        )
        assert app_connection.execute(COUNT_PROBES_SQL).fetchone() == (1,)
    with connection.cursor() as owner_cursor:
        owner_cursor.execute(
            """
            SELECT count(*) FROM pg_catalog.pg_policies
            WHERE schemaname = 'clinic_app'
              AND policyname <> 'tenant_isolation'
            """
        )
        assert owner_cursor.fetchone() == (0,)
