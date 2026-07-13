from __future__ import annotations

from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from django.db import DatabaseError, connection, transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from otp_test_support import create_totp_device, current_user_guc, runtime_role

if TYPE_CHECKING:
    from conftest import TenantGraph

POLICY_NAME: Final = "otp_totp_device_user_isolation"
POLICY_EXPRESSION: Final = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_totp_table_is_force_rls_owned_by_non_runtime_role() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, owner.rolname, "
            "app.rolsuper, app.rolbypassrls "
            "FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_roles AS owner ON owner.oid = c.relowner "
            "JOIN pg_catalog.pg_roles AS app ON app.rolname = 'clinic_app' "
            "WHERE c.oid = 'clinic_app.otp_totp_totpdevice'::regclass"
        )
        posture = cursor.fetchone()
        cursor.execute(
            "SELECT permissive, roles, cmd, qual, with_check "
            "FROM pg_catalog.pg_policies "
            "WHERE schemaname = 'clinic_app' "
            "AND tablename = 'otp_totp_totpdevice' "
            "AND policyname = %s",
            [POLICY_NAME],
        )
        policy = cursor.fetchone()

    assert posture == (True, True, "clinic_owner", False, False)
    assert policy is not None
    assert policy[:3] == ("PERMISSIVE", ["clinic_app"], "ALL")
    assert "app.current_user_id" in policy[3]
    assert "app.current_user_id" in policy[4]
    assert "NULLIF" in policy[3].upper()
    assert "NULLIF" in policy[4].upper()


def test_totp_seed_acl_is_limited_to_owner_and_runtime_role() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT class.relname, COALESCE(role.rolname, 'PUBLIC'), "
            "acl.privilege_type "
            "FROM pg_catalog.pg_class AS class "
            "CROSS JOIN LATERAL "
            "pg_catalog.aclexplode(class.relacl) AS acl "
            "LEFT JOIN pg_catalog.pg_roles AS role ON role.oid = acl.grantee "
            "WHERE class.oid IN ("
            "'clinic_app.otp_totp_totpdevice'::regclass, "
            "'clinic_app.otp_totp_totpdevice_id_seq'::regclass)"
        )
        grants = set(cursor.fetchall())

    table = "otp_totp_totpdevice"
    sequence = "otp_totp_totpdevice_id_seq"
    assert grants == {
        *(
            (table, "clinic_app", privilege)
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
            )
        ),
        *(
            (table, "clinic_owner", privilege)
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            )
        ),
        *(
            (sequence, "clinic_app", privilege)
            for privilege in (
                "SELECT",
                "USAGE",
            )
        ),
        *(
            (sequence, "clinic_owner", privilege)
            for privilege in (
                "SELECT",
                "UPDATE",
                "USAGE",
            )
        ),
    }


def test_forced_rls_hides_seed_from_table_owner(tenant_graph: TenantGraph) -> None:
    foreign = create_totp_device(tenant_graph.user_a, confirmed=True)

    assert not TOTPDevice.objects.filter(pk=foreign.pk).exists()


def test_clinic_app_can_crud_only_its_own_device(tenant_graph: TenantGraph) -> None:
    with runtime_role(), current_user_guc(tenant_graph.user_a):
        own = TOTPDevice.objects.create(
            user_id=tenant_graph.user_a,
            name="Own authenticator",
            confirmed=False,
        )
        own.name = "Renamed authenticator"
        own.save(update_fields=("name",))
        assert TOTPDevice.objects.get(pk=own.pk).name == "Renamed authenticator"
        deleted, _ = TOTPDevice.objects.filter(pk=own.pk).delete()

    assert deleted == 1


def test_cross_user_seed_and_mutations_are_fail_closed(
    tenant_graph: TenantGraph,
) -> None:
    foreign = create_totp_device(tenant_graph.user_a, confirmed=True)

    with runtime_role(), current_user_guc(tenant_graph.user_b):
        assert not TOTPDevice.objects.filter(pk=foreign.pk).exists()
        assert TOTPDevice.objects.filter(pk=foreign.pk).update(name="stolen") == 0
        deleted, _ = TOTPDevice.objects.filter(pk=foreign.pk).delete()
        assert deleted == 0
        with transaction.atomic(), pytest.raises(DatabaseError):
            TOTPDevice.objects.create(
                user_id=tenant_graph.user_a,
                name="Cross-user insert",
                confirmed=False,
            )


@pytest.mark.parametrize("setting", [None, ""])
def test_totp_queries_return_zero_for_unset_or_empty_user_guc(
    tenant_graph: TenantGraph,
    setting: str | None,
) -> None:
    create_totp_device(tenant_graph.user_a, confirmed=True)

    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        if setting is None:
            cursor.execute("RESET app.current_user_id")
        else:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [setting],
            )
        assert TOTPDevice.objects.count() == 0


def test_permissive_policy_toggle_proves_cross_user_test_has_teeth(
    tenant_graph: TenantGraph,
    superuser_database_url: str,
) -> None:
    foreign = create_totp_device(tenant_graph.user_a, confirmed=True)

    with runtime_role(), current_user_guc(tenant_graph.user_b):
        assert not TOTPDevice.objects.filter(pk=foreign.pk).exists()

    permissive_sql = (
        "ALTER POLICY "
        f"{POLICY_NAME} ON clinic_app.otp_totp_totpdevice "
        "USING (true) WITH CHECK (true)"
    )
    restore_sql = (
        "ALTER POLICY "
        f"{POLICY_NAME} ON clinic_app.otp_totp_totpdevice "
        f"USING ({POLICY_EXPRESSION}) WITH CHECK ({POLICY_EXPRESSION})"
    )
    try:
        with psycopg.connect(superuser_database_url) as raw_connection:
            raw_connection.execute(permissive_sql)
        with runtime_role(), current_user_guc(tenant_graph.user_b):
            exposed = TOTPDevice.objects.get(pk=foreign.pk)
            assert exposed.key == foreign.key
    finally:
        with psycopg.connect(superuser_database_url) as raw_connection:
            raw_connection.execute(restore_sql)
