"""Reception acceptance against real staff roles, RLS and database room guards."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.services import create_invoice, issue_invoice
from apps.consent.services import publish_text
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.questionnaires import assign_for_appointment, publish_template
from apps.scheduling.appointment_lifecycle import complete, start
from apps.scheduling.models import (
    Appointment,
    ClinicianDelay,
    DisplayDevice,
    QueueEntry,
    Resource,
    RoomStatus,
)
from apps.scheduling.reception import (
    ReceptionAccessDeniedError,
    ReceptionConflictError,
    ReceptionInputError,
    arrival_readiness,
    broadcast_clinician_delay,
    call_to_room,
    issue_display_device,
    mark_arrived,
    revoke_display_device,
    update_room_status,
)
from apps.scheduling.reception_queries import view_reception_day
from apps.scheduling.resource_services import ResourceInput, create_resource
from apps.tenancy.db import tenant_context
from django.db import connection, connections, transaction
from django.test import Client
from django.utils.translation import gettext

from identity.permission_support import owner_context, permission_actor
from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class ReceptionSetup:
    """Synthetic receptionist, appointment and two administrative resources."""

    booking: AppointmentSetup
    appointment: Appointment
    location: Resource
    room: Resource


def _setup(graph: RbacGraph) -> ReceptionSetup:
    booking = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(booking.actor_id, booking.organization_id):
        appointment = create_synthetic_appointment(booking)
        location = create_resource(
            clinic_id=booking.clinic_id,
            content=ResourceInput(name="Synthetic reception", kind="location"),
        )
        room = create_resource(
            clinic_id=booking.clinic_id,
            content=ResourceInput(name="Synthetic room", kind="room"),
        )
    return ReceptionSetup(booking, appointment, location, room)


def _arrive(setup: ReceptionSetup, *, command_id: UUID | None = None) -> QueueEntry:
    return mark_arrived(
        clinic_id=setup.booking.clinic_id,
        appointment_id=setup.appointment.pk,
        location_id=setup.location.pk,
        expected_revision=setup.appointment.revision,
        command_id=command_id or uuid4(),
    )


def test_arrival_enqueues_once_and_server_computes_readiness(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    command = uuid4()
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        entry = _arrive(setup, command_id=command)
        replay = _arrive(setup, command_id=command)
        readiness = arrival_readiness(
            clinic_id=setup.booking.clinic_id, appointment_id=setup.appointment.pk
        )
        assert replay.pk == entry.pk
        assert QueueEntry.objects.filter(appointment=setup.appointment).count() == 1
        assert entry.position == 1
        assert entry.ticket_code == "A1"
        assert readiness.forms_pending == 0
        assert readiness.payment_pending is False
        assert readiness.identity_document_recorded is False


def test_nurse_calls_arrived_ticket_without_gaining_appointment_moves(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    nurse, _ = permission_actor(rbac_graph, "nurse")
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        entry = _arrive(setup)
    with runtime_role(), tenant_context(nurse, setup.booking.organization_id):
        with pytest.raises(CurrentActorError):
            require_permission("appointment.move", clinic_id=setup.booking.clinic_id)
        with pytest.raises(ReceptionAccessDeniedError):
            issue_display_device(clinic_id=setup.booking.clinic_id)
        called = call_to_room(
            clinic_id=setup.booking.clinic_id,
            queue_entry_id=entry.pk,
            room_id=setup.room.pk,
            room_unit=1,
            expected_revision=entry.revision,
        )
        assert called.state == "called"
        assert called.called_at is not None
        with pytest.raises(ReceptionConflictError):
            update_room_status(
                clinic_id=setup.booking.clinic_id,
                room_id=setup.room.pk,
                unit=1,
                state="cleaning",
                expected_revision=0,
            )
    with owner_context(setup.booking.organization_id):
        setup.appointment.refresh_from_db()
        assert setup.appointment.status == "arrived"


def test_readiness_counts_real_pending_work_without_blocking_arrival(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    administrator, _ = permission_actor(rbac_graph, "clinic_admin")
    with runtime_role(), tenant_context(administrator, setup.booking.organization_id):
        template = publish_template(
            clinic_id=setup.booking.clinic_id,
            key="reception-readiness",
            title="Synthetic readiness",
            questions=[
                {
                    "id": "q_prepared",
                    "label": "Synthetic question",
                    "type": "boolean",
                    "required": True,
                    "max_length": 5,
                    "options": [],
                }
            ],
        )
        for _ in range(2):
            publish_text(
                clinic_id=setup.booking.clinic_id,
                purpose="consultation_recording",
                text="Synthetic optional recording permission.",
            )
    with (
        runtime_role(),
        tenant_context(setup.booking.practitioner_id, setup.booking.organization_id),
    ):
        assign_for_appointment(
            clinic_id=setup.booking.clinic_id,
            appointment_id=setup.appointment.pk,
            template_id=template.pk,
        )
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        invoice = create_invoice(
            clinic_id=setup.booking.clinic_id,
            patient_id=setup.booking.patient_id,
            appointment_id=setup.appointment.pk,
            amount_minor=12345,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=setup.booking.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )
        readiness = arrival_readiness(
            clinic_id=setup.booking.clinic_id, appointment_id=setup.appointment.pk
        )
        assert readiness.forms_pending == 1
        assert readiness.consents_pending == 1
        assert readiness.payment_pending is True
        assert _arrive(setup).state == "waiting"
    nurse, _ = permission_actor(rbac_graph, "nurse")
    with (
        runtime_role(),
        tenant_context(nurse, setup.booking.organization_id),
        pytest.raises(ReceptionAccessDeniedError),
    ):
        arrival_readiness(
            clinic_id=setup.booking.clinic_id, appointment_id=setup.appointment.pk
        )


def test_delay_broadcast_has_revision_and_role_boundaries(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        delay = broadcast_clinician_delay(
            clinic_id=setup.booking.clinic_id,
            practitioner_id=setup.booking.practitioner_id,
            minutes=15,
            expected_revision=0,
        )
        screen = view_reception_day(clinic_id=setup.booking.clinic_id)
        assert [(row.pk, row.minutes) for row in screen.delays] == [(delay.pk, 15)]
        with pytest.raises(ReceptionConflictError):
            broadcast_clinician_delay(
                clinic_id=setup.booking.clinic_id,
                practitioner_id=setup.booking.practitioner_id,
                minutes=20,
                expected_revision=0,
            )
        with pytest.raises(ReceptionInputError):
            broadcast_clinician_delay(
                clinic_id=setup.booking.clinic_id,
                practitioner_id=setup.booking.practitioner_id,
                minutes=721,
                expected_revision=delay.revision,
            )
    nurse, _ = permission_actor(rbac_graph, "nurse")
    with (
        runtime_role(),
        tenant_context(nurse, setup.booking.organization_id),
        pytest.raises(ReceptionAccessDeniedError),
    ):
        broadcast_clinician_delay(
            clinic_id=setup.booking.clinic_id,
            practitioner_id=setup.booking.practitioner_id,
            minutes=20,
            expected_revision=delay.revision,
        )


def test_nurse_cannot_register_arrival_for_known_or_unknown_appointment(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    nurse, _ = permission_actor(rbac_graph, "nurse")
    with runtime_role(), tenant_context(nurse, setup.booking.organization_id):
        for appointment_id in (setup.appointment.pk, uuid4()):
            with pytest.raises(ReceptionAccessDeniedError):
                mark_arrived(
                    clinic_id=setup.booking.clinic_id,
                    appointment_id=appointment_id,
                    location_id=setup.location.pk,
                    expected_revision=1,
                    command_id=uuid4(),
                )
    with owner_context(setup.booking.organization_id):
        setup.appointment.refresh_from_db()
        assert setup.appointment.status == "scheduled"
        assert not QueueEntry.objects.exists()


def test_clinician_progress_updates_queue_in_the_same_transaction(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        entry = _arrive(setup)
        call_to_room(
            clinic_id=setup.booking.clinic_id,
            queue_entry_id=entry.pk,
            room_id=setup.room.pk,
            room_unit=1,
            expected_revision=entry.revision,
        )
    with (
        runtime_role(),
        tenant_context(setup.booking.practitioner_id, setup.booking.organization_id),
    ):
        setup.appointment.refresh_from_db()
        active = start(
            clinic_id=setup.booking.clinic_id,
            appointment_id=setup.appointment.pk,
            expected_revision=setup.appointment.revision,
            command_id=uuid4(),
        )
        entry.refresh_from_db()
        assert entry.state == "in_room"
        complete(
            clinic_id=setup.booking.clinic_id,
            appointment_id=active.pk,
            expected_revision=active.revision,
            command_id=uuid4(),
        )
        entry.refresh_from_db()
        assert entry.state == "done"


def test_public_projection_requires_live_token_and_contains_only_codes(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        entry = _arrive(setup)
        call_to_room(
            clinic_id=setup.booking.clinic_id,
            queue_entry_id=entry.pk,
            room_id=setup.room.pk,
            room_unit=1,
            expected_revision=entry.revision,
        )
        issued = issue_display_device(clinic_id=setup.booking.clinic_id)
    digest = hashlib.sha256(issued.token.encode("ascii")).hexdigest()
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.reception_display(%s)", [digest])
        projection = json.loads(cursor.fetchone()[0])
        assert set(projection) == {"tickets"}
        assert len(projection["tickets"]) == 1
        ticket = projection["tickets"][0]
        assert set(ticket) == {"ticket", "room", "state"}
        assert ticket["ticket"] == entry.ticket_code
        assert ticket["room"].startswith("R")
        assert "Synthetic" not in str(projection)
        cursor.execute("SELECT clinic_app.reception_display(%s)", ["0" * 64])
        assert cursor.fetchone() == (None,)
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        revoke_display_device(
            clinic_id=setup.booking.clinic_id, device_id=issued.device_id
        )
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.reception_display(%s)", [digest])
        assert cursor.fetchone() == (None,)


def test_unknown_and_foreign_queue_selectors_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        entry = _arrive(setup)
        for clinic, selector in (
            (setup.booking.clinic_id, uuid4()),
            (rbac_graph.clinic_b, entry.pk),
        ):
            with pytest.raises(ReceptionAccessDeniedError):
                call_to_room(
                    clinic_id=clinic,
                    queue_entry_id=selector,
                    room_id=setup.room.pk,
                    room_unit=1,
                    expected_revision=entry.revision,
                )
        entry.refresh_from_db()
        assert entry.state == "waiting"
        assert entry.revision == 1


def test_display_refusals_ignore_staff_cookie_and_revocation_is_immediate(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    staff, _ = receptionist_client(rbac_graph)
    anonymous = Client()
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        issued = issue_display_device(clinic_id=setup.booking.clinic_id)
    with runtime_role():
        absent = anonymous.get("/waiting-display/")
        forged = anonymous.get(
            "/waiting-display/", HTTP_AUTHORIZATION="Bearer " + "x" * 43
        )
        with_staff_cookie = staff.get(
            "/waiting-display/", HTTP_AUTHORIZATION="Bearer " + "x" * 43
        )
        active = anonymous.get(
            "/waiting-display/", HTTP_AUTHORIZATION="Bearer " + issued.token
        )
    assert active.status_code == 200
    assert set(active["Cache-Control"].split(", ")) == {
        "no-store",
        "private",
        "no-cache",
        "must-revalidate",
    }
    assert {
        response.status_code
        for response in (
            absent,
            forged,
            with_staff_cookie,
        )
    } == {403}
    assert absent.content == forged.content == with_staff_cookie.content
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        revoke_display_device(
            clinic_id=setup.booking.clinic_id, device_id=issued.device_id
        )
    with runtime_role():
        revoked = anonymous.get(
            "/waiting-display/", HTTP_AUTHORIZATION="Bearer " + issued.token
        )
    assert revoked.status_code == 403
    assert revoked.content == absent.content
    assert gettext("Device disconnected").encode() in revoked.content


def test_every_reception_table_hides_rows_from_another_tenant(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        entry = _arrive(setup)
        call_to_room(
            clinic_id=setup.booking.clinic_id,
            queue_entry_id=entry.pk,
            room_id=setup.room.pk,
            room_unit=1,
            expected_revision=entry.revision,
        )
        delay = broadcast_clinician_delay(
            clinic_id=setup.booking.clinic_id,
            practitioner_id=setup.booking.practitioner_id,
            minutes=10,
            expected_revision=0,
        )
        room = RoomStatus.objects.get(room_id=setup.room.pk, unit=1)
        device = issue_display_device(clinic_id=setup.booking.clinic_id)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not QueueEntry.objects.filter(pk=entry.pk).exists()
        assert not RoomStatus.objects.filter(pk=room.pk).exists()
        assert not ClinicianDelay.objects.filter(pk=delay.pk).exists()
        assert not DisplayDevice.objects.filter(pk=device.device_id).exists()
        assert (
            DisplayDevice.objects.filter(pk=device.device_id).update(active=False) == 0
        )
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        assert DisplayDevice.objects.get(pk=device.device_id).active is True


def test_two_calls_for_one_room_have_exactly_one_winner(
    rbac_graph: RbacGraph,
) -> None:
    setup = _setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(setup.booking.actor_id, setup.booking.organization_id),
    ):
        first = _arrive(setup)
        second_appointment = create_synthetic_appointment(
            setup.booking,
            start_local="2035-06-02T10:00",
            end_local="2035-06-02T11:00",
        )
        second = mark_arrived(
            clinic_id=setup.booking.clinic_id,
            appointment_id=second_appointment.pk,
            location_id=setup.location.pk,
            expected_revision=second_appointment.revision,
            command_id=uuid4(),
        )
    barrier = Barrier(2, timeout=20)

    def call(entry: QueueEntry) -> str:
        connections.close_all()
        try:
            with (
                runtime_role(),
                tenant_context(setup.booking.actor_id, setup.booking.organization_id),
            ):
                barrier.wait()
                try:
                    call_to_room(
                        clinic_id=setup.booking.clinic_id,
                        queue_entry_id=entry.pk,
                        room_id=setup.room.pk,
                        room_unit=1,
                        expected_revision=entry.revision,
                    )
                except ReceptionConflictError:
                    return "conflict"
                return "called"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(call, (first, second)))
    assert sorted(outcomes) == ["called", "conflict"]
