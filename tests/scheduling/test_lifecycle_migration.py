"""Migration 0007 rehearsal: populated upgrade, empty reverse, populated refusal.

One schema transaction widens ``status`` and rewrites both exclusions; a legacy
booking survives unapply -> reapply unchanged, and once any lifecycle v2 state
exists the reverse refuses (rollback = restore, never a history rewrite).
"""

from __future__ import annotations

from difflib import SequenceMatcher
from importlib import import_module
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.ehr.migrations import _arrival_sql as arrival
from apps.scheduling.migrations import _expiry_audit_sql as expiry_audit
from apps.scheduling.migrations import _lifecycle_sql as lifecycle
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from database_urls import database_url_for_name
from patient_service_support import runtime_role
from patients.test_intake_migrations import _default_connection, _scratch_database
from rbac_fixtures import rbac_graph as historical_graph
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.backends.postgresql.base import DatabaseWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("historical_graph",)

V1 = ("scheduling", "0006_capacity_insert_authorization")
V2 = ("scheduling", "0007_appointment_lifecycle_v2")
OCCUPYING = (
    "'held'::character varying, 'scheduled'::character varying, "
    "'arrived'::character varying, 'in_progress'::character varying"
)


def _exclusion_predicates() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid="
            "'clinic_app.scheduling_appointment'::regclass AND contype='x' "
            "AND conname LIKE 'scheduling_appointment_scheduled_%'"
        )
        return {row[0].split(" WHERE ", 1)[1] for row in cursor.fetchall()}


def _constraint(error: IntegrityError) -> str | None:
    cause = error.__cause__
    assert isinstance(cause, psycopg.Error)
    return cause.diag.constraint_name


@pytest.fixture
def historical_database(
    superuser_database_url: str, request: pytest.FixtureRequest
) -> Iterator[tuple[RbacGraph, DatabaseWrapper, str]]:
    """Keep the default alias restored before the normal teardown hook begins."""
    with _scratch_database(superuser_database_url) as wrapper:
        with _default_connection(wrapper):
            executor = MigrationExecutor(connection)
            # Current seeding needs identity, duplicate and alias prerequisites.
            # Later intake leaves depend on scheduling successors under test.
            executor.migrate(
                [
                    V2,
                    ("intake", "0017_patient_aliases"),
                    *executor.loader.graph.leaf_nodes("identity"),
                ]
            )
            graph = request.getfixturevalue("historical_graph")
        yield (
            graph,
            wrapper,
            database_url_for_name(
                superuser_database_url, wrapper.settings_dict["NAME"]
            ),
        )


def test_populated_upgrade_and_rollback_refusal(
    historical_database: tuple[RbacGraph, DatabaseWrapper, str],
) -> None:
    rbac_graph, wrapper, superuser_database_url = historical_database
    with _default_connection(wrapper):
        setup = seed_appointment_setup(rbac_graph)
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            legacy = create_synthetic_appointment(setup)
        with psycopg.connect(superuser_database_url, autocommit=True) as superuser:

            def snapshot() -> tuple[object, ...]:
                row = superuser.execute(
                    "SELECT status, start_at, end_at, practitioner_id, patient_id "
                    "FROM clinic_app.scheduling_appointment WHERE id=%s",
                    [legacy.pk],
                ).fetchone()
                assert row is not None
                return tuple(row)

            before = snapshot()
            # Empty-v2 reverse: only booked/cancelled rows exist, so 0007 unapplies.
            MigrationExecutor(connection).migrate([V1])
            assert _exclusion_predicates() == {"(((status)::text = 'scheduled'::text))"}
            assert snapshot() == before
            # Populated upgrade: the legacy booking survives with v2 defaults.
            executor = MigrationExecutor(connection)
            executor.migrate([V2])
            historical_apps = executor.loader.project_state([V2]).apps
            assert snapshot() == before
            assert _exclusion_predicates() == {
                f"(((status)::text = ANY ((ARRAY[{OCCUPYING}])::text[])))"
            }
            defaults = superuser.execute(
                "SELECT revision, hold_expires_at, series_id, authorization_reference, "
                "payer_membership_id FROM clinic_app.scheduling_appointment "
                "WHERE id=%s",
                [legacy.pk],
            ).fetchone()
            assert defaults == (1, None, None, "", None)
            # Populate the historical table as the migration-test superuser.
            # The current service requires later reception tables; ACLs stay closed.
            appointment_model = historical_apps.get_model("scheduling", "Appointment")
            superuser.execute(
                "SELECT set_config('app.current_user_id', %s, false), "
                "set_config('app.current_tenant', %s, false)",
                [str(setup.actor_id), str(setup.organization_id)],
            )
            populated = superuser.execute(
                sql.SQL(
                    "UPDATE clinic_app.{} SET status=%s, revision=%s "
                    "WHERE id=%s RETURNING status, revision"
                ).format(sql.Identifier(appointment_model._meta.db_table)),
                ["arrived", 2, legacy.pk],
            ).fetchone()
            assert populated == ("arrived", 2)
        # Once a v2 state exists, the reverse refuses and nothing is rewritten.
        with pytest.raises(IntegrityError) as refused:
            MigrationExecutor(connection).migrate([V1])
        assert _constraint(refused.value) == "scheduling_lifecycle_rollback"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM django_migrations WHERE app='scheduling' "
                "AND name='0007_appointment_lifecycle_v2'"
            )
            assert cursor.fetchone() == (1,)


def _hunks(source: str, derived: str) -> int:
    matcher = SequenceMatcher(None, source.splitlines(), derived.splitlines())
    return sum(1 for tag, *_ in matcher.get_opcodes() if tag != "equal")


def test_every_derived_sql_edit_actually_matched() -> None:
    """A drifted source makes a .replace() a silent no-op; this fails instead."""
    patient_booking = import_module("apps.scheduling.migrations.0003_patient_booking")
    assert lifecycle._GUARD_V1 == patient_booking.PATIENT_GUARD  # live 0003 body
    current_membership = import_module(
        "apps.identity.migrations._cut2_current_membership_sql"
    )
    # Identity 0031 (todo 15) is the live capacity predecessor: its revoked-role
    # filter survives the lifecycle edit, and the reverse restores that body.
    assert lifecycle._CAPACITY.removesuffix("\n") in current_membership.SQL
    assert "role.revoked_at IS NULL" in lifecycle._CAPACITY_V2
    pairs = {
        # (source, derived): exact number of edited hunks
        "guard_v1": (lifecycle._GUARD_V1, lifecycle._GUARD_V1_V2, 1),
        "patient_guard": (lifecycle._PATIENT_GUARD, lifecycle._PATIENT_GUARD_V2, 1),
        "patient_receipt": (
            lifecycle._PATIENT_RECEIPT,
            lifecycle._PATIENT_RECEIPT_V2,
            1,
        ),
        "capacity": (lifecycle._CAPACITY, lifecycle._CAPACITY_V2, 3),
        "expire_hold": (expiry_audit.EXPIRE_HOLD, expiry_audit.EXPIRE_HOLD_GATED, 2),
    }
    # 0008 reverses to 0007's W expiry byte for byte.
    assert (
        expiry_audit.EXPIRE_HOLD.removeprefix("CREATE OR REPLACE ")
        in lifecycle._GUARD_SQL_PART_8
    )
    for name, (source, derived, hunks) in pairs.items():
        assert source.startswith("CREATE OR REPLACE FUNCTION clinic_app."), name
        assert _hunks(source, derived) == hunks, name
    assert "__ENCOUNTER_STATUSES__" not in arrival.SQL + arrival.REVERSE_SQL
    assert _hunks(arrival.REVERSE_SQL, arrival.SQL) == 1
    assert "IN ('arrived', 'in_progress')" in arrival.SQL
    protected = import_module("apps.ehr.migrations.0009_protected_fields")
    body = arrival.REVERSE_SQL.removeprefix("SET LOCAL ROLE clinic_resolver;\n")
    assert body.removesuffix("RESET ROLE;\n").strip() in protected._GUARD_SQL
    # No empty separator parts remain in the assembled statements.
    for order in (lifecycle._GUARD_SQL_ORDER, lifecycle._REVERSE_GUARD_SQL_ORDER):
        assert all(part.strip() for part in order)
