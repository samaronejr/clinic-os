"""Executable exceptions for lifecycle actor readers derived from the catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.db import DatabaseError, connection, transaction

from scheduling.lifecycle_world import acting, pinned_clock

if TYPE_CHECKING:
    from collections.abc import Callable

    from scheduling.lifecycle_world import LifecycleWorld


def due_holds(world: LifecycleWorld, database_url: str) -> None:
    held = world.rows["held"]
    assert held.hold_expires_at is not None
    with pinned_clock(database_url, held.hold_expires_at):
        for name in ("receptionist", "inactive_receptionist", "machine"):
            with acting(world, world.actors[name]), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT appointment_id FROM clinic_app.scheduling_due_holds(100)"
                )
                rows = cursor.fetchall()
                assert (held.pk in {row[0] for row in rows}) == (name == "machine")


def expire_hold(world: LifecycleWorld, database_url: str) -> None:
    held = world.rows["held"]
    assert held.hold_expires_at is not None
    with pinned_clock(database_url, held.hold_expires_at):
        for name in ("receptionist", "inactive_receptionist", "machine"):
            with acting(world, world.actors[name]):
                if name == "machine":
                    with transaction.atomic(), connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT clinic_app.scheduling_expire_hold(%s,%s,%s,%s)",
                            [held.clinic_id, held.pk, held.revision, uuid4()],
                        )
                        assert cursor.fetchone() == (held.revision + 1,)
                else:
                    with (
                        pytest.raises(DatabaseError) as error,
                        transaction.atomic(),
                        connection.cursor() as cursor,
                    ):
                        cursor.execute(
                            "SELECT clinic_app.scheduling_expire_hold(%s,%s,%s,%s)",
                            [held.clinic_id, held.pk, held.revision, uuid4()],
                        )
                    assert getattr(error.value.__cause__, "sqlstate", None) == "42501"


def lifecycle_receipt(world: LifecycleWorld, _database_url: str) -> None:
    row = world.rows["scheduled"]
    with acting(world, world.actors["receptionist"]), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.scheduling_appointment SET status='arrived' WHERE id=%s",
            [row.pk],
        )
        assert cursor.rowcount == 1
        cursor.execute(
            "SELECT actor_kind FROM clinic_app.scheduling_appointmenttransition "
            "WHERE appointment_id=%s AND to_status='arrived'",
            [row.pk],
        )
        assert cursor.fetchall() == [("staff",)]


def lifecycle_guard(world: LifecycleWorld, _database_url: str) -> None:
    row = world.rows["scheduled"]
    actor = world.actors["receptionist"].user_id
    assert actor is not None
    for active in (True, False, True):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
                [active, actor],
            )
            assert cursor.rowcount == 1
            cursor.execute("SET LOCAL ROLE clinic_owner")
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true), "
                "set_config('app.current_user_id',%s,true)",
                [str(world.graph.organization_a), str(actor)],
            )
            if active:
                cursor.execute(
                    "UPDATE clinic_app.scheduling_appointment "
                    "SET status='arrived' WHERE id=%s",
                    [row.pk],
                )
                assert cursor.rowcount == 1
            else:
                with pytest.raises(DatabaseError) as error, transaction.atomic():
                    cursor.execute(
                        "UPDATE clinic_app.scheduling_appointment "
                        "SET status='arrived' WHERE id=%s",
                        [row.pk],
                    )
                assert getattr(error.value.__cause__, "sqlstate", None) == "42501"
            transaction.set_rollback(True)


def release_due_holds(world: LifecycleWorld, database_url: str) -> None:
    held = world.rows["held"]
    assert held.hold_expires_at is not None
    with (
        pinned_clock(database_url, held.hold_expires_at),
        acting(world, world.actors["receptionist"]),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute("SELECT current_setting('app.current_user_id',true)")
        before = cursor.fetchone()
        cursor.execute(
            "SELECT clinic_app.scheduling_release_due_holds(%s,%s,%s,%s,%s,%s,%s)",
            [
                uuid4(),
                held.organization_id,
                held.practitioner_id,
                held.patient_id,
                held.resource_ids,
                held.start_at,
                held.end_at,
            ],
        )
        cursor.execute("SELECT current_setting('app.current_user_id',true)")
        assert cursor.fetchone() == before
        cursor.execute(
            "SELECT status FROM clinic_app.scheduling_appointment WHERE id=%s",
            [held.pk],
        )
        assert cursor.fetchone() == ("expired",)
        cursor.execute(
            "SELECT actor_kind FROM clinic_app.scheduling_appointmenttransition "
            "WHERE appointment_id=%s AND to_status='expired'",
            [held.pk],
        )
        assert cursor.fetchall() == [("machine",)]


LIFECYCLE_PROBES: dict[str, Callable[[LifecycleWorld, str], None]] = {
    "clinic_app.scheduling_due_holds(integer)": due_holds,
    "clinic_app.scheduling_expire_hold(uuid,uuid,integer,uuid)": expire_hold,
    "clinic_app.scheduling_lifecycle_receipt()": lifecycle_receipt,
    "clinic_app.scheduling_release_due_holds(uuid,uuid,uuid,uuid,uuid[],"
    "timestamp with time zone,timestamp with time zone)": release_due_holds,
}

LIFECYCLE_DELEGATE_PROBES: dict[str, Callable[[LifecycleWorld, str], None]] = {
    "clinic_app.scheduling_lifecycle_guard()": lifecycle_guard,
}
