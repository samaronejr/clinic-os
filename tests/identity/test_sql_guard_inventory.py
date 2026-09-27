"""Census completeness is independent of the hand-written SQL behavior probes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import connection, transaction

from identity import legacy_sql_inventory, test_service_principals
from identity.legacy_guard_inventory import discover_sql
from identity.legacy_sql_inventory import assert_sql_inventory
from identity.sql_guard_probes import ALL_ROLES, PROBES
from identity.test_metrics_guard_parity import METRICS_SQL_ORACLES
from identity.test_permission_parity import INVENTORY
from workflows import test_sql_authority as workflow_sql

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_sql_inventory_classifications_have_executable_role_oracles(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = INVENTORY["sql_guards"]
    assert_sql_inventory(entries)
    listed = set()
    operation_oracles = {}
    machine_oracles = {}
    workflow_oracles = {}
    for name, entry in entries.items():
        if entry["kind"] == "staff_guard":
            assert len(entry["discovery"]["signatures"]) == 1, name
            assert entry["probes"], name
            for key in entry["probes"]:
                probe = PROBES[key]
                assert name == f"clinic_app.{probe.name}"
                assert probe.roles
                assert set(probe.roles) <= set(ALL_ROLES)
                listed.add(key)
        elif entry["kind"] == "operations_aggregate":
            assert entry["reason"], name
            assert entry["probes"], name
            operation_oracles[name] = entry["probes"]
        elif entry["kind"] == "machine_principal":
            assert entry["reason"], name
            assert entry["probes"], name
            machine_oracles[name] = entry["probes"]
        elif entry["kind"] == "workflow_protocol":
            workflow_oracles[name] = entry["probes"]
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
    assert workflow_oracles == {
        name: ["test_every_workflow_sql_member_for_every_catalog_role"]
        for name in workflow_sql.workflow_sql_members()
    }
    assert set(workflow_oracles) == set(workflow_sql.SQL_CASES)
    workflow_sql.test_every_workflow_sql_member_for_every_catalog_role(
        rbac_graph, monkeypatch
    )
    assert listed == set(PROBES)
    assert operation_oracles == METRICS_SQL_ORACLES
    # Membership is the registry label itself; the oracles derive their member
    # set from it, so every labelled function is executed, not hand-listed.
    assert sorted(machine_oracles) == test_service_principals.machine_members()
    for name, oracles in machine_oracles.items():
        assert oracles == test_service_principals.MACHINE_ORACLES, name
    for oracle in test_service_principals.MACHINE_ORACLES:
        assert callable(getattr(test_service_principals, oracle)), oracle


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
