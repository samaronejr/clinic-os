from __future__ import annotations

import importlib
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
    "(user_id = (NULLIF(current_setting("
    "'app.current_user_id'::text, true), ''::text))::uuid)"
)
EXPECTED_POLICY: Final = (
    POLICY_NAME,
    "ALL",
    ["clinic_app"],
    "PERMISSIVE",
    POLICY_EXPRESSION,
    POLICY_EXPRESSION,
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
            "SELECT policyname, cmd, roles, permissive, qual, with_check "
            "FROM pg_catalog.pg_policies "
            "WHERE schemaname = 'clinic_app' "
            "AND tablename = 'otp_totp_totpdevice' "
            "ORDER BY policyname"
        )
        policies = cursor.fetchall()

    assert posture == (True, True, "clinic_owner", False, False)
    assert policies == [EXPECTED_POLICY]


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
        *((sequence, "clinic_app", privilege) for privilege in ("USAGE",)),
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


def test_foreign_unconfirmed_enrollment_seed_is_fail_closed(
    tenant_graph: TenantGraph,
) -> None:
    foreign = create_totp_device(tenant_graph.user_a, confirmed=False)

    with runtime_role(), current_user_guc(tenant_graph.user_b):
        assert not TOTPDevice.objects.filter(pk=foreign.pk).exists()


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


def test_extra_permissive_policy_proves_unconfirmed_isolation_has_teeth(
    tenant_graph: TenantGraph,
    superuser_database_url: str,
) -> None:
    foreign = create_totp_device(tenant_graph.user_a, confirmed=False)

    with runtime_role(), current_user_guc(tenant_graph.user_b):
        assert not TOTPDevice.objects.filter(pk=foreign.pk).exists()

    with psycopg.connect(superuser_database_url) as raw_connection:
        try:
            raw_connection.execute(
                "CREATE POLICY test_unconfirmed_device_leak "
                "ON clinic_app.otp_totp_totpdevice "
                "AS PERMISSIVE FOR SELECT TO clinic_app "
                "USING (NOT confirmed)"
            )
            raw_connection.execute("SET LOCAL ROLE clinic_app")
            raw_connection.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [str(tenant_graph.user_b)],
            )
            exposed = raw_connection.execute(
                "SELECT key FROM clinic_app.otp_totp_totpdevice WHERE id = %s",
                [foreign.pk],
            ).fetchone()
            assert exposed == (foreign.key,)
        finally:
            raw_connection.rollback()


def test_totp_rls_migration_reverse_and_reapply_restore_exact_catalogs(
    superuser_database_url: str,
) -> None:
    migration = importlib.import_module("apps.identity.migrations.0003_totp_device_rls")
    posture_sql = (
        "SELECT class.relrowsecurity, class.relforcerowsecurity, "
        "class.relowner::regrole::text "
        "FROM pg_catalog.pg_class AS class "
        "WHERE class.oid = "
        "'clinic_app.otp_totp_totpdevice'::pg_catalog.regclass"
    )
    policies_sql = (
        "SELECT policyname, cmd, roles, permissive, qual, with_check "
        "FROM pg_catalog.pg_policies "
        "WHERE schemaname = 'clinic_app' "
        "AND tablename = 'otp_totp_totpdevice' ORDER BY policyname"
    )
    acl_sql = (
        "SELECT class.relname, COALESCE(grantee.rolname, 'PUBLIC'), "
        "grantor.rolname, acl.privilege_type, acl.is_grantable "
        "FROM pg_catalog.pg_class AS class "
        "CROSS JOIN LATERAL pg_catalog.aclexplode(class.relacl) AS acl "
        "LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee "
        "JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = acl.grantor "
        "WHERE class.oid IN ("
        "'clinic_app.otp_totp_totpdevice'::pg_catalog.regclass, "
        "'clinic_app.otp_totp_totpdevice_id_seq'::pg_catalog.regclass)"
    )

    with psycopg.connect(superuser_database_url) as raw_connection:
        try:
            raw_connection.execute(migration.REVERSE_SQL)
            reversed_posture = raw_connection.execute(posture_sql).fetchone()
            reversed_policies = raw_connection.execute(policies_sql).fetchall()
            reversed_acl = set(raw_connection.execute(acl_sql).fetchall())
            raw_connection.execute(migration.FORWARD_SQL)
            reapplied_posture = raw_connection.execute(posture_sql).fetchone()
            reapplied_policies = raw_connection.execute(policies_sql).fetchall()
            reapplied_acl = set(raw_connection.execute(acl_sql).fetchall())
        finally:
            raw_connection.rollback()

    table = "otp_totp_totpdevice"
    sequence = "otp_totp_totpdevice_id_seq"
    expected_acl = {
        *(
            (table, "clinic_owner", "clinic_owner", privilege, False)
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
            (table, "clinic_app", "clinic_owner", privilege, False)
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        ),
        *(
            (sequence, "clinic_owner", "clinic_owner", privilege, False)
            for privilege in ("SELECT", "UPDATE", "USAGE")
        ),
        (sequence, "clinic_app", "clinic_owner", "USAGE", False),
    }
    assert reversed_posture == (False, False, "clinic_owner")
    assert reversed_policies == []
    assert reversed_acl == expected_acl
    assert reapplied_posture == (True, True, "clinic_owner")
    assert reapplied_policies == [EXPECTED_POLICY]
    assert reapplied_acl == expected_acl
