"""Migration 0006 rehearsal: populated upgrade, empty reverse, populated refusal.

One schema transaction widens ``status`` and rewrites both exclusions; a legacy
booking survives unapply -> reapply unchanged, and once any lifecycle v2 state
exists the reverse refuses (rollback = restore, never a history rewrite).
"""

from __future__ import annotations

from difflib import SequenceMatcher
from importlib import import_module
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ehr.migrations import _arrival_sql as arrival
from apps.scheduling.migrations import _lifecycle_sql as lifecycle
from apps.scheduling.services import arrive
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

V1 = ("scheduling", "0005_resources_templates")
V2 = ("scheduling", "0006_appointment_lifecycle_v2")
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


def test_populated_upgrade_and_rollback_refusal(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
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
        # Empty-v2 reverse: only booked/cancelled rows exist, so 0006 unapplies.
        MigrationExecutor(connection).migrate([V1])
        assert _exclusion_predicates() == {"(((status)::text = 'scheduled'::text))"}
        assert snapshot() == before
        # Populated upgrade: the legacy booking survives with v2 defaults.
        MigrationExecutor(connection).migrate([V2])
        assert snapshot() == before
        assert _exclusion_predicates() == {
            f"(((status)::text = ANY ((ARRAY[{OCCUPYING}])::text[])))"
        }
        defaults = superuser.execute(
            "SELECT revision, hold_expires_at, series_id, authorization_reference, "
            "payer_membership_id FROM clinic_app.scheduling_appointment WHERE id=%s",
            [legacy.pk],
        ).fetchone()
        assert defaults == (1, None, None, "", None)
    # Once a v2 state exists, the reverse refuses and nothing is rewritten.
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        arrived = arrive(
            clinic_id=setup.clinic_id,
            appointment_id=legacy.pk,
            expected_revision=1,
            command_id=uuid4(),
        )
    assert arrived.status == "arrived"
    with pytest.raises(IntegrityError) as refused:
        MigrationExecutor(connection).migrate([V1])
    assert _constraint(refused.value) == "scheduling_lifecycle_rollback"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations WHERE app='scheduling' "
            "AND name='0006_appointment_lifecycle_v2'"
        )
        assert cursor.fetchone() == (1,)


def _hunks(source: str, derived: str) -> int:
    matcher = SequenceMatcher(None, source.splitlines(), derived.splitlines())
    return sum(1 for tag, *_ in matcher.get_opcodes() if tag != "equal")


def test_every_derived_sql_edit_actually_matched() -> None:
    """A drifted source makes a .replace() a silent no-op; this fails instead."""
    patient_booking = import_module("apps.scheduling.migrations.0003_patient_booking")
    assert lifecycle._GUARD_V1 == patient_booking.PATIENT_GUARD  # live 0003 body
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
    }
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
