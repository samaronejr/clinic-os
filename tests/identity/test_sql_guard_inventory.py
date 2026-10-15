"""Census completeness is independent of the hand-written SQL behavior probes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from django.db import connection, transaction

from auth.stepup_test_support import create_role_actor
from identity import legacy_sql_inventory, test_service_principals
from identity.domain_sql_oracles import assert_executable_protocol_oracles
from identity.legacy_guard_inventory import discover_sql
from identity.legacy_sql_inventory import assert_sql_inventory
from identity.sql_guard_probes import ALL_ROLES, PROBES
from identity.test_lifecycle_sql_guards import STAFF_PROTOCOL_GUARDS
from identity.test_lifecycle_sql_readers import (
    SQL_PROTOCOL_ORACLES,
    assert_lifecycle_oracles,
)
from identity.test_metrics_guard_parity import METRICS_SQL_ORACLES
from identity.test_permission_parity import INVENTORY
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from identity.legacy_sql_inventory import SqlInventoryEntry
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_sql_inventory_classifications_have_executable_role_oracles() -> None:
    entries = INVENTORY["sql_guards"]
    assert_sql_inventory(entries)
    assert_staff_role_oracles(entries)


def assert_staff_role_oracles(entries: dict[str, SqlInventoryEntry]) -> None:
    """Check the role-probe registry without replacing live catalog validation."""
    listed = set()
    for name, entry in entries.items():
        if entry["kind"] != "staff_guard":
            continue
        assert len(entry["discovery"]["signatures"]) == 1, name
        assert entry["probes"], name
        for key in entry["probes"]:
            probe = PROBES[key]
            assert name == f"clinic_app.{probe.name}"
            assert probe.roles
            assert set(probe.roles) <= set(ALL_ROLES)
            listed.add(key)
    assert listed == set(PROBES)


def test_sql_protocol_inventory_has_exact_executable_oracles() -> None:
    entries = INVENTORY["sql_guards"]
    assert_sql_inventory(entries)
    assert_protocol_oracles(entries)


def assert_protocol_oracles(entries: dict[str, SqlInventoryEntry]) -> None:
    """Keep classification and callable bindings independently host-free."""
    operation_oracles = {}
    machine_oracles = {}
    protocol_oracles = {}
    staff_protocols = set()
    lifecycle_protocols = {}
    for name, entry in (
        (name, entry)
        for name, entry in entries.items()
        if entry["kind"] != "staff_guard"
    ):
        if entry["kind"] == "domain_protocol":
            assert entry["reason"], name
            assert entry["probes"], name
            protocol_oracles[name] = entry["probes"]
        elif entry["kind"] == "operations_aggregate":
            assert entry["reason"], name
            assert entry["probes"], name
            operation_oracles[name] = entry["probes"]
        elif entry["kind"] == "machine_principal":
            assert entry["reason"], name
            assert entry["probes"], name
            machine_oracles[name] = entry["probes"]
        elif entry["kind"] == "data_operation":
            assert name == "clinic_app.unaccent"
            assert entry["probes"] == [
                "test_unaccent_normalization_is_role_independent"
            ]
            assert entry["reason"]
        elif entry["kind"] == "staff_protocol":
            assert entry["probes"] == ["test_current_staff_protocol_guards"], name
            assert entry["reason"], name
            staff_protocols.add(name)
        elif entry["kind"] == "lifecycle_protocol":
            assert entry["reason"], name
            assert entry["probes"], name
            lifecycle_protocols[name] = tuple(entry["probes"])
        else:
            assert entry["kind"] in {
                "migration_only",
                "row_integrity",
                "audit_protocol",
                "crypto_protocol",
                "patient_principal",
                "public_verification",
                "worker_protocol",
                "data_allocator",
            }, name
            assert entry["reason"], name
            assert not entry["probes"], name
    assert staff_protocols == set(STAFF_PROTOCOL_GUARDS)
    assert lifecycle_protocols == SQL_PROTOCOL_ORACLES
    assert_lifecycle_oracles(SQL_PROTOCOL_ORACLES.values())
    assert operation_oracles == METRICS_SQL_ORACLES
    assert_executable_protocol_oracles(protocol_oracles)
    # Membership is the registry label itself; the oracles derive their member
    # set from it, so every labelled function is executed, not hand-listed.
    assert sorted(machine_oracles) == test_service_principals.machine_members()
    for name, oracles in machine_oracles.items():
        assert oracles == test_service_principals.MACHINE_ORACLES, name
    for oracle in test_service_principals.MACHINE_ORACLES:
        assert callable(getattr(test_service_principals, oracle)), oracle


@pytest.mark.parametrize("role", ALL_ROLES)
def test_unaccent_normalization_is_role_independent(
    rbac_graph: RbacGraph,
    role: str,
) -> None:
    actor = create_role_actor(rbac_graph, UserClinicRole.Role(role))
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        for user_id, tenant_id in (
            (str(actor.pk), str(rbac_graph.organization_a)),
            ("", ""),
            (str(uuid4()), "not-a-uuid"),
            ("not-a-uuid", str(rbac_graph.organization_a)),
        ):
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true),"
                "set_config('app.current_tenant',%s,true)",
                [user_id, tenant_id],
            )
            cursor.execute(
                "SELECT clinic_app.unaccent(%s::text),"
                "clinic_app.unaccent('clinic_app.unaccent'::regdictionary,%s::text)",
                ["S\u00ednt\u00e9tico", "S\u00ednt\u00e9tico"],
            )
            assert cursor.fetchone() == ("Sintetico", "Sintetico")
        transaction.set_rollback(True)


def test_omitting_a_deployed_sql_guard_breaks_the_inventory() -> None:
    incomplete = dict(INVENTORY["sql_guards"])
    del incomplete["clinic_app.teleconsult_assigned"]
    with pytest.raises(AssertionError):
        assert_sql_inventory(incomplete)


@pytest.mark.parametrize("root", ["policy", "resolver", "parsed_resolver"])
def test_live_unlisted_invoker_guard_is_discovered(root: str) -> None:
    # No SECURITY DEFINER flag on the guard itself, and no Python caller or
    # migration declaration: it must be found through the independent roots.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION public.sql_parity_unlisted() RETURNS boolean
            LANGUAGE sql AS $$ SELECT false $$
        """)
        if root == "policy":
            cursor.execute("CREATE TABLE public.sql_parity_subject(id integer)")
            cursor.execute(
                "ALTER TABLE public.sql_parity_subject ENABLE ROW LEVEL SECURITY"
            )
            cursor.execute("""
                CREATE POLICY sql_parity_policy ON public.sql_parity_subject
                USING (public.sql_parity_unlisted())
            """)
        elif root == "parsed_resolver":
            cursor.execute("""
                CREATE FUNCTION clinic_app.sql_parity_resolver() RETURNS boolean
                LANGUAGE sql SECURITY DEFINER
                BEGIN ATOMIC
                    SELECT public.sql_parity_unlisted();
                END
            """)
        else:
            cursor.execute("""
                CREATE FUNCTION clinic_app.sql_parity_resolver() RETURNS boolean
                LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public
                AS $$ SELECT public.sql_parity_unlisted() $$
            """)
        discovered = discover_sql()
        assert "public.sql_parity_unlisted" in discovered
        assert (
            discovered["public.sql_parity_unlisted"]["signatures"][0][
                "security_definer"
            ]
            is False
        )
        with pytest.raises(AssertionError):
            assert_sql_inventory(INVENTORY["sql_guards"])
        transaction.set_rollback(True)


def test_private_migration_sql_is_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "apps/example/migrations/_authority_sql.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'SQL = """CREATE OR REPLACE FUNCTION clinic_app.source_only_guard(id uuid) '
        'RETURNS boolean LANGUAGE sql AS $$ SELECT false $$;"""'
    )
    monkeypatch.setattr(legacy_sql_inventory, "ROOT", tmp_path)
    assert legacy_sql_inventory.migration_definitions() == {
        "clinic_app.source_only_guard": ["apps/example/migrations/_authority_sql.py"],
    }
