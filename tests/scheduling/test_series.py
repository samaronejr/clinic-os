"""Recurring series (todo 22): bounded rules, 'this'/'this and future' edits.

Occurrences are ordinary appointments bound by (series, index); edits never
delete them. Clinic-local wall time is kept per date: across the date Brazil
used to start DST (first Sunday of November before 2019), America/Sao_Paulo has
no transition under the pinned tzdata, so the UTC instant never shifts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.scheduling.appointment_series import series_dates
from apps.scheduling.models import Appointment, AppointmentSeries, SeriesException
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentIdempotencyConflictError,
    AppointmentLifecycleError,
    AppointmentLifecycleInputError,
    AppointmentLocalRange,
    SeriesBooking,
    SeriesEdit,
    SlotConflict,
    create_appointment,
    create_availability,
    create_series,
    edit_series,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]

ZONE = "America/Sao_Paulo"
# Weekly Wednesdays across 2035-11-04, the old first-Sunday-of-November DST start.
FIRST = "2035-10-24"
WEEKS = 5
RANGE = AppointmentLocalRange(f"{FIRST}T09:00", f"{FIRST}T09:30")
# Pinned series edit authority: SM S actor + RP agenda row (never from code).
EDIT_TERMS = (("appointment.move", False), ("appointment.move_own", True))
CREATE_TERMS = (("appointment.book", False), ("appointment.book_own", True))


def _setup(graph: RbacGraph) -> AppointmentSetup:
    setup = seed_appointment_setup(graph)
    with owner_context(graph.organization_a):
        assert Clinic.objects.get(pk=graph.clinic_a).timezone == ZONE
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        for week in range(WEEKS):
            day = date.fromisoformat(FIRST) + timedelta(weeks=week)
            create_availability(
                clinic_id=setup.clinic_id,
                practitioner_id=setup.practitioner_id,
                start_local=f"{day}T08:00",
                end_local=f"{day}T12:00",
                idempotency_key=uuid4(),
            )
    return setup


def _weekly(setup: AppointmentSetup, key: UUID | None = None) -> AppointmentSeries:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        return create_series(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            booking=SeriesBooking(
                AppointmentLocalRange(f"{FIRST}T09:00", f"{FIRST}T09:30"),
                "weekly",
                count=WEEKS,
            ),
            idempotency_key=key or uuid4(),
        )


def _occurrences(series: AppointmentSeries) -> list[Appointment]:
    with owner_context(series.organization_id):
        return list(Appointment.objects.filter(series=series).order_by("series_index"))


def test_rule_subset_is_bounded_and_monthly_uses_nth_weekday() -> None:
    first = date(2035, 10, 24)  # fourth Wednesday
    assert series_dates(first, SeriesBooking(RANGE, "weekly", count=3)) == (
        first,
        first + timedelta(weeks=1),
        first + timedelta(weeks=2),
    )
    assert series_dates(first, SeriesBooking(RANGE, "biweekly", count=2))[1] == (
        first + timedelta(weeks=2)
    )
    monthly = series_dates(first, SeriesBooking(RANGE, "monthly", count=3))
    assert monthly == (date(2035, 10, 24), date(2035, 11, 28), date(2035, 12, 26))
    assert all(day.weekday() == first.weekday() for day in monthly)
    until = series_dates(
        first,
        SeriesBooking(RANGE, "weekly", until=first + timedelta(days=20)),
    )
    assert len(until) == 3
    for invalid in (
        SeriesBooking(RANGE, "daily", count=2),
        SeriesBooking(RANGE, "weekly"),
        SeriesBooking(RANGE, "weekly", count=2, until=first),
        SeriesBooking(RANGE, "weekly", count=53),
        SeriesBooking(RANGE, "weekly", until=first + timedelta(days=400)),
    ):
        with pytest.raises(AppointmentLifecycleInputError):
            series_dates(first, invalid)
    with pytest.raises(AppointmentLifecycleInputError):
        # A fifth weekday does not exist every month.
        series_dates(date(2035, 10, 31), SeriesBooking(RANGE, "monthly", count=2))


def test_series_materializes_atomically_with_stable_local_time(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    key = uuid4()
    series = _weekly(setup, key)
    rows = _occurrences(series)
    assert [row.series_index for row in rows] == list(range(1, WEEKS + 1))
    # 09:00 in Sao Paulo is 12:00Z on every date, before and after 2035-11-04.
    assert {(row.start_at.hour, row.start_at.minute) for row in rows} == {(12, 0)}
    assert all(row.start_at.tzinfo is not None for row in rows)
    assert [row.start_at.date() for row in rows] == [
        date.fromisoformat(FIRST) + timedelta(weeks=week) for week in range(WEEKS)
    ]
    assert {row.status for row in rows} == {"scheduled"}
    # Equal replay returns the same series; a different rule under the key
    # conflicts; nothing new is written either way.
    assert _weekly(setup, key).pk == series.pk
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        pytest.raises(AppointmentIdempotencyConflictError),
    ):
        create_series(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            booking=SeriesBooking(
                AppointmentLocalRange(f"{FIRST}T09:00", f"{FIRST}T09:30"),
                "weekly",
                count=WEEKS - 1,
            ),
            idempotency_key=key,
        )
    assert len(_occurrences(series)) == WEEKS


def test_one_conflicting_occurrence_rejects_the_whole_series(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    third = date.fromisoformat(FIRST) + timedelta(weeks=2)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        create_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            local_range=AppointmentLocalRange(f"{third}T09:00", f"{third}T09:30"),
            idempotency_key=uuid4(),
        )
    with pytest.raises(SlotConflict):
        _weekly(setup)
    with owner_context(setup.organization_id):
        assert not AppointmentSeries.objects.exists()
        assert not Appointment.objects.filter(series__isnull=False).exists()


def _edit(
    setup: AppointmentSetup,
    series: AppointmentSeries,
    edit: SeriesEdit,
    actor: UUID | None = None,
    command: UUID | None = None,
) -> tuple[Appointment, ...]:
    with runtime_role(), tenant_context(actor or setup.actor_id, setup.organization_id):
        return edit_series(
            clinic_id=setup.clinic_id,
            series_id=series.pk,
            edit=edit,
            command_id=command or uuid4(),
        )


def test_this_and_future_move_keeps_wall_time_across_the_old_dst_date(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    series = _weekly(setup)
    before = _occurrences(series)
    command = uuid4()
    moved = _edit(
        setup, series, SeriesEdit("moved", 2, "future", "10:00"), None, command
    )
    after = _occurrences(series)
    assert [row.pk for row in after] == [row.pk for row in before]  # none deleted
    assert after[0].start_at == before[0].start_at
    for old, new in zip(before[1:], after[1:], strict=True):
        assert new.start_at == old.start_at + timedelta(hours=1)
        assert (new.start_at.hour, new.start_at.minute) == (13, 0)
        assert new.end_at - new.start_at == old.end_at - old.start_at
    assert [row.pk for row in moved] == [row.pk for row in after[1:]]
    with owner_context(setup.organization_id):
        receipt = SeriesException.objects.get(series=series)
    assert (receipt.scope, receipt.kind, receipt.occurrence_index) == (
        "future",
        "moved",
        2,
    )
    assert receipt.affected_count == WEEKS - 1
    # Replaying the command changes nothing; reusing it differently conflicts.
    _edit(setup, series, SeriesEdit("moved", 2, "future", "10:00"), None, command)
    with pytest.raises(AppointmentIdempotencyConflictError):
        _edit(setup, series, SeriesEdit("moved", 3, "this", "11:00"), None, command)
    assert [row.start_at for row in _occurrences(series)] == [
        row.start_at for row in after
    ]


def test_this_occurrence_cancel_then_future_cancel_never_deletes(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    series = _weekly(setup)
    _edit(setup, series, SeriesEdit("cancelled", 2, "this"))
    rows = _occurrences(series)
    assert [row.status for row in rows] == [
        "scheduled",
        "cancelled",
        "scheduled",
        "scheduled",
        "scheduled",
    ]
    _edit(setup, series, SeriesEdit("cancelled", 4, "future"))
    rows = _occurrences(series)
    assert len(rows) == WEEKS
    assert [row.status for row in rows] == [
        "scheduled",
        "cancelled",
        "scheduled",
        "cancelled",
        "cancelled",
    ]
    assert {row.cancellation_reason for row in rows if row.status == "cancelled"} == {
        "clinic_request"
    }
    # Nothing actionable left at index 5: an edit there is an illegal transition.
    with pytest.raises(AppointmentLifecycleError) as illegal:
        _edit(setup, series, SeriesEdit("cancelled", 5, "this"))
    assert illegal.value.code == "illegal_transition"
    with (
        owner_context(setup.organization_id),
        connection.cursor() as cursor,
        pytest.raises(Exception, match="immutable"),
        transaction.atomic(),
    ):
        cursor.execute(
            "DELETE FROM clinic_app.scheduling_seriesexception WHERE series_id=%s",
            [series.pk],
        )


def _edit_allowed(role: str, own: bool) -> bool:
    return any(
        permission in BUNDLES_V1[role] and (own or not own_only)
        for permission, own_only in EDIT_TERMS
    )


def test_series_edit_authority_by_every_role_and_owner_both_ways(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    series = _weekly(setup)
    actors = {
        role: permission_actor(rbac_graph, role)[0]
        for role in UserClinicRole.Role.values
    }
    cases = [(role, actor, False) for role, actor in actors.items()]
    cases.append(("physician", rbac_graph.physician, True))  # the series owner
    for role, actor, own in cases:
        expected = _edit_allowed(role, own)
        try:
            with (
                runtime_role(),
                tenant_context(actor, setup.organization_id),
                transaction.atomic(),
            ):
                edit_series(
                    clinic_id=setup.clinic_id,
                    series_id=series.pk,
                    edit=SeriesEdit("cancelled", 1, "this"),
                    command_id=uuid4(),
                )
                transaction.set_rollback(True)
            decided = True
        except AppointmentAccessDeniedError:
            decided = False
        assert decided is expected, (role, own)
    # A foreign clinic id and an unknown series are refused identically.
    refusals = set()
    for clinic_id, series_id in (
        (rbac_graph.clinic_b, series.pk),
        (setup.clinic_id, uuid4()),
    ):
        try:
            with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
                edit_series(
                    clinic_id=clinic_id,
                    series_id=series_id,
                    edit=SeriesEdit("cancelled", 1, "this"),
                    command_id=uuid4(),
                )
        except AppointmentAccessDeniedError as error:
            refusals.add(str(error))
    assert len(refusals) == 1
    assert {row.status for row in _occurrences(series)} == {"scheduled"}


def test_series_creation_authority_by_every_role(rbac_graph: RbacGraph) -> None:
    setup = _setup(rbac_graph)
    cases = [
        (role, permission_actor(rbac_graph, role)[0], False)
        for role in UserClinicRole.Role.values
    ]
    cases.append(("physician", rbac_graph.physician, True))  # own schedule
    for role, actor, own in cases:
        expected = any(
            permission in BUNDLES_V1[role] and (own or not own_only)
            for permission, own_only in CREATE_TERMS
        )
        try:
            with (
                runtime_role(),
                tenant_context(actor, setup.organization_id),
                transaction.atomic(),
            ):
                create_series(
                    clinic_id=setup.clinic_id,
                    enrollment_id=setup.enrollment_id,
                    practitioner_id=setup.practitioner_id,
                    booking=SeriesBooking(
                        AppointmentLocalRange(f"{FIRST}T09:00", f"{FIRST}T09:30"),
                        "weekly",
                        count=2,
                    ),
                    idempotency_key=uuid4(),
                )
                transaction.set_rollback(True)
            decided = True
        except AppointmentAccessDeniedError:
            decided = False
        assert decided is expected, (role, own)


def test_reference_clock_is_before_every_occurrence(resource_clock: datetime) -> None:
    assert resource_clock < datetime.fromisoformat(f"{FIRST}T00:00").replace(tzinfo=UTC)
