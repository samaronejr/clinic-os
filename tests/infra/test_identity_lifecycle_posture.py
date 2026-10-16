"""Real catalog and direct-runtime refusal for retained lifecycle authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from infra.identity_lifecycle_catalog import TABLES
from otp_test_support import create_totp_device, get_totp_device, runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_every_lifecycle_table_has_forced_rls_and_no_runtime_delete() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity,relowner::regrole::text "
            "FROM pg_class WHERE relnamespace='clinic_app'::regnamespace "
            "AND relname=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' AND grantee IN "
            "('clinic_app','clinic_agent','PUBLIC') AND table_name=ANY(%s) "
            "AND privilege_type IN ('DELETE','TRUNCATE','REFERENCES','TRIGGER')",
            [list(TABLES)],
        )
        assert cursor.fetchall() == []


def test_owner_writers_have_exact_narrow_execution_and_search_path() -> None:
    writers = {
        ("identity_invitation_account", "requested_clinic uuid, requested_user uuid"),
        ("identity_invitation_accept", "requested_hash bytea, requested_username text"),
        (
            "identity_invitation_password",
            "requested_hash bytea, requested_username bytea, requested_password text, "
            "requested_device_key text",
        ),
        ("identity_offboarding_revoke", "requested_user uuid, requested_clinic uuid"),
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname,pg_get_function_identity_arguments(p.oid),"
            "p.proowner::regrole::text,p.prosecdef,p.proconfig,"
            "ARRAY(SELECT coalesce(r.rolname,'PUBLIC') FROM aclexplode("
            "coalesce(p.proacl,acldefault('f',p.proowner))) a "
            "LEFT JOIN pg_roles r ON r.oid=a.grantee "
            "WHERE a.privilege_type='EXECUTE' ORDER BY 1) "
            "FROM pg_proc p WHERE p.pronamespace='clinic_app'::regnamespace "
            "AND p.proname=ANY(%s)",
            [[name for name, _signature in writers]],
        )
        rows = cursor.fetchall()
    assert {(row[0], row[1]) for row in rows} == writers
    for row in rows:
        assert row[2:] == (
            "clinic_owner",
            True,
            ["search_path=pg_catalog, pg_temp"],
            ["clinic_app", "clinic_owner"],
        )


def test_runtime_cannot_call_internal_authenticator_retirement(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    device = create_totp_device(graph.clinic_admin, confirmed=True)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.identity_retire_authenticators(%s)",
                [graph.clinic_admin],
            )
        assert get_totp_device(graph.clinic_admin, confirmed=True).pk == device.pk
