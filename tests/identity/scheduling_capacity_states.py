"""Live capacity-trigger dimensions and isolated, rolled-back phase execution."""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING

from apps.scheduling.models import Appointment
from django.db import connection, transaction
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

CAPACITY = "clinic_app.scheduling_capacity_guard()"
STATUS_DECISION = re.compile(
    r"\bNEW\.status\s*(?:IN\s*\(([^)]+)\)|(?:=|<>)\s*'([^']+)')",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True, order=True)
class CapacityState:
    operation: str
    phase: str
    status: str
    service: bool
    consumes: bool


def capacity_states() -> frozenset[CapacityState]:
    """Derive the population from the deployed body, triggers and model vocabulary."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", [CAPACITY])
        row = cursor.fetchone()
        assert row is not None
        source = str(row[0])
        cursor.execute(
            "SELECT tgtype FROM pg_trigger WHERE tgfoid=%s::regprocedure "
            "AND NOT tgisinternal AND tgenabled='O'",
            [CAPACITY],
        )
        trigger_types = [int(row[0]) for row in cursor.fetchall()]
    phases = set()
    for flags in trigger_types:
        # Row INSERT/UPDATE only; DELETE, TRUNCATE and INSTEAD OF fail closed.
        assert flags & 1, flags
        assert flags & ~(1 | 2 | 4 | 16) == 0, flags
        phase = "BEFORE" if flags & 2 else "AFTER"
        for operation, bit in (("INSERT", 4), ("UPDATE", 16)):
            if flags & bit:
                phases.add((operation, phase))
    assert phases
    for variable, vocabulary in (
        ("TG_OP", {operation for operation, _ in phases}),
        ("TG_WHEN", {phase for _, phase in phases}),
    ):
        decisions = list(
            re.finditer(rf"\b{variable}\s*=\s*'([^']+)'", source, re.IGNORECASE)
        )
        assert {match[1] for match in decisions} <= vocabulary
        for read in re.finditer(rf"\b{variable}\b", source, re.IGNORECASE):
            assert any(
                match.start() <= read.start() < match.end() for match in decisions
            ), ("unclassified trigger decision", variable)
    decisions = list(STATUS_DECISION.finditer(source))
    for read in re.finditer(r"\bNEW\.status\b", source, re.IGNORECASE):
        assert any(
            match.start() <= read.start() < match.end() for match in decisions
        ), "unclassified status decision"
    statuses = set(Appointment.Status.values)
    for match in decisions:
        statuses.update(re.findall(r"'([^']+)'", match[1] or ""))
        if match[2]:
            statuses.add(match[2])
    consuming = re.findall(
        r"\bconsumes\s*:=\s*NEW\.status\s+IN\s*\(([^)]+)\)", source, re.IGNORECASE
    )
    assert len(consuming) == 1, "unclassified capacity-state decision"
    consumes = frozenset(re.findall(r"'([^']+)'", consuming[0]))
    assert Appointment._meta.get_field("service_type").null
    return frozenset(
        CapacityState(operation, phase, status, service, status in consumes)
        for (operation, phase), status, service in product(
            phases, statuses, (False, True)
        )
    )


@contextmanager
def capacity_phase(
    booked: Appointment, state: CapacityState
) -> Iterator[Callable[[], int]]:
    """Execute the real trigger with real NEW/OLD rows at each deployed phase.

    A temporary row carrier omits unrelated lifecycle/exclusion guards, which
    otherwise reject the capacity body's future status branches before AFTER.
    The parent appointment and resources are real; all reservations, temporary
    grants and DDL roll back. Normal-table DML is independently checked too.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(booked.organization_id)],
        )
        cursor.execute(
            "CREATE TEMP TABLE capacity_probe (LIKE clinic_app.scheduling_appointment)"
        )
        # Plan item 22 owns lifecycle widening. The guard names in_progress,
        # longer than the present appointment varchar(10).
        cursor.execute("ALTER TABLE capacity_probe ALTER COLUMN status TYPE text")
        values = {}
        for field in Appointment._meta.concrete_fields:
            column = field.column
            assert column is not None
            values[column] = getattr(booked, field.attname)
        if not state.service:
            values.update(
                service_type_id=None, resource_ids=[], buffer_before=0, buffer_after=0
            )
        values["status"] = state.status
        values["cancellation_reason"] = (
            "clinic_request" if state.status == "cancelled" else None
        )
        values["cancelled_at"] = (
            booked.created_at if state.status == "cancelled" else None
        )
        insert = sql.SQL("INSERT INTO capacity_probe ({}) VALUES ({})").format(
            sql.SQL(",").join(sql.Identifier(column) for column in values),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        )
        if state.operation == "UPDATE":
            cursor.execute(insert, list(values.values()))
        cursor.execute("GRANT SELECT,INSERT,UPDATE ON capacity_probe TO clinic_app")
        cursor.execute("GRANT TRIGGER ON capacity_probe TO clinic_resolver")
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            f"CREATE TRIGGER capacity_probe_trigger {state.phase} {state.operation} "
            "ON capacity_probe FOR EACH ROW EXECUTE FUNCTION "
            "clinic_app.scheduling_capacity_guard()"
        )
        cursor.execute("RESET ROLE")
        if state.phase == "AFTER" and state.service:
            # Make the real allocation/release distinguishable from a trigger
            # that was never reached; the entire change rolls back.
            cursor.execute(
                "UPDATE clinic_app.scheduling_appointmentresource SET occupied=%s "
                "WHERE appointment_id=%s",
                [not state.consumes, booked.pk],
            )

        def statement() -> int:
            with connection.cursor() as current:
                if state.operation == "INSERT":
                    current.execute(insert, list(values.values()))
                else:
                    current.execute("UPDATE capacity_probe SET updated_at=updated_at")
                affected = int(current.rowcount)
                if state.phase == "AFTER" and state.service:
                    current.execute(
                        "SELECT occupied FROM "
                        "clinic_app.scheduling_appointmentresource "
                        "WHERE appointment_id=%s",
                        [booked.pk],
                    )
                    occupancy = [row[0] for row in current.fetchall()]
                    assert occupancy
                    assert occupancy == [state.consumes] * len(occupancy)
                return affected

        yield statement
        transaction.set_rollback(True)
