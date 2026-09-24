"""Patient authority, hidden-scope tampering and shared scheduling guarantees."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.intake.models import PatientAccessGrant, PatientSession
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient, issue_invitation
from apps.scheduling.models import Appointment, AvailabilityBlock, PatientBookingEvent
from apps.scheduling.patient_booking import (
    book_patient_slot,
    cancel_patient_appointment,
    patient_appointment,
    patient_appointments,
    patient_slots,
    reschedule_patient_appointment,
)
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentLocalRange,
    AppointmentPractitionerError,
    AppointmentTerminalError,
    SlotConflict,
    create_appointment,
    retire_availability,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from django.test import Client
from django.utils import timezone

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2035, 6, 2)
URL = "/patient/appointments/"


def _session(setup: AppointmentSetup) -> UUID:
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        grant = issue_invitation(
            clinic_id=setup.clinic_id, enrollment_id=setup.enrollment_id
        )
    with runtime_role():
        session = redeem_invitation(setup.clinic_id, grant.secret)
    assert session is not None
    return session


def _client(session_id: UUID) -> Client:
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(session_id)
    session.save()
    return client


def test_patient_books_replays_reschedules_and_cancels_without_staff(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    key = uuid4()
    with runtime_role(), patient_session_context(session_id):
        slots = patient_slots(DAY)
        assert len(slots) == 8
        original = book_patient_slot(token=slots[0].token, idempotency_key=key)
        assert (
            book_patient_slot(token=slots[0].token, idempotency_key=key).pk
            == original.pk
        )
        moved = reschedule_patient_appointment(original.pk, slots[2].token)
        assert moved.pk == original.pk
        assert moved.start_at == slots[2].start_at
        assert (
            reschedule_patient_appointment(original.pk, slots[2].token).pk
            == original.pk
        )
        cancelled = cancel_patient_appointment(original.pk)
        assert (
            cancel_patient_appointment(original.pk).cancelled_at
            == cancelled.cancelled_at
        )
        assert cancelled.status == "cancelled"
        replay = book_patient_slot(token=slots[0].token, idempotency_key=key)
        assert replay.status == "cancelled"
        assert replay.start_at == moved.start_at
        with pytest.raises(AppointmentTerminalError):
            reschedule_patient_appointment(original.pk, slots[4].token)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT NULLIF(current_setting('app.current_tenant',true),''), "
                "NULLIF(current_setting('app.current_user_id',true),'')"
            )
            assert cursor.fetchone() == (None, None)
        assert len(patient_appointments()) == 1
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert list(
            PatientBookingEvent.objects.order_by("created_at").values_list(
                "action", flat=True
            )
        ) == ["created", "rescheduled", "cancelled"]
        assert set(
            PatientBookingEvent.objects.values_list("patient_session_id", flat=True)
        ) == {session_id}


def test_mismatched_key_and_corrupt_token_do_not_write(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    with runtime_role(), patient_session_context(session_id):
        slots = patient_slots(DAY)
        key = uuid4()
        book_patient_slot(token=slots[0].token, idempotency_key=key)
        with pytest.raises(AppointmentIdempotencyConflictError):
            book_patient_slot(token=slots[1].token, idempotency_key=key)
        with pytest.raises(AppointmentCreateInputError):
            book_patient_slot(
                token=slots[1].token + "tampered", idempotency_key=uuid4()
            )
        assert Appointment.objects.count() == 1


def test_stale_availability_rechecked_and_failed_key_reusable(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    with runtime_role(), patient_session_context(session_id):
        token = patient_slots(DAY)[0].token
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        block = AvailabilityBlock.objects.get(clinic_id=setup.clinic_id)
        retire_availability(clinic_id=setup.clinic_id, availability_id=block.pk)
    with runtime_role(), patient_session_context(session_id):
        assert patient_slots(DAY) == ()
        with pytest.raises(AppointmentAvailabilityError):
            book_patient_slot(token=token, idempotency_key=uuid4())
        assert Appointment.objects.count() == 0


def test_enrollment_clinic_and_practitioner_tampering_is_denied(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    client = _client(session_id)
    with runtime_role(), patient_session_context(session_id):
        token = patient_slots(DAY)[0].token
        with pytest.raises(AppointmentAccessDeniedError):
            create_appointment(
                clinic_id=setup.clinic_id,
                enrollment_id=uuid4(),
                practitioner_id=setup.practitioner_id,
                local_range=AppointmentLocalRange(
                    "2035-06-02T08:00", "2035-06-02T08:30"
                ),
                idempotency_key=uuid4(),
            )
        with pytest.raises(AppointmentAccessDeniedError):
            create_appointment(
                clinic_id=rbac_graph.clinic_b,
                enrollment_id=setup.enrollment_id,
                practitioner_id=setup.practitioner_id,
                local_range=AppointmentLocalRange(
                    "2035-06-02T08:00", "2035-06-02T08:30"
                ),
                idempotency_key=uuid4(),
            )
    for field in ("enrollment_id", "practitioner_id", "patient_id", "clinic_id"):
        with runtime_role():
            response = client.post(
                URL,
                {
                    "action": "book",
                    "slot": token,
                    "idempotency_key": str(uuid4()),
                    field: str(uuid4()),
                },
            )
        assert response.status_code == 403
    with runtime_role(), patient_session_context(session_id):
        assert Appointment.objects.count() == 0


def test_other_appointments_hidden_and_other_session_token_denied(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    first = _session(setup)
    second = _session(setup)
    with runtime_role(), patient_session_context(first):
        token = patient_slots(DAY)[0].token
    with runtime_role(), patient_session_context(second):
        with pytest.raises(AppointmentAccessDeniedError):
            book_patient_slot(token=token, idempotency_key=uuid4())
        with pytest.raises(AppointmentAccessDeniedError):
            patient_appointment(uuid4())
        with pytest.raises(AppointmentAccessDeniedError):
            patient_slots(DAY, uuid4())
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        # Staff-created rows for this patient remain visible and transitionable.
        own = create_synthetic_appointment(setup)
    with runtime_role(), patient_session_context(first):
        assert patient_appointment(own.pk).pk == own.pk
        assert len(patient_slots(DAY)) == 6


def test_operation_and_revocation_fail_closed(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(setup.organization_id)],
        )
        PatientSession.objects.filter(pk=session_id).update(
            operations=["enrollment_view"]
        )
    with runtime_role(), patient_session_context(session_id):
        with pytest.raises(AppointmentAccessDeniedError):
            patient_slots(DAY)
        assert Appointment.objects.count() == 0
    with runtime_role():
        assert _client(session_id).get(URL).status_code == 403
        assert Client().get(URL).status_code == 403
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        pytest.raises(AppointmentAccessDeniedError),
    ):
        patient_slots(DAY)


def test_native_http_book_replay_view_and_cancel(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    client = _client(session_id)
    with runtime_role(), patient_session_context(session_id):
        token = patient_slots(DAY)[0].token
    body = {
        "action": "book",
        "slot": token,
        "idempotency_key": str(uuid4()),
        "day": DAY.isoformat(),
    }
    with runtime_role():
        assert client.get(URL, {"day": DAY.isoformat()}).status_code == 200
        assert client.post(URL, body).status_code == 303
        assert client.post(URL, body).status_code == 303
        response = client.get(URL)
        assert response.status_code == 200
    with runtime_role(), patient_session_context(session_id):
        appointment = patient_appointments()[0]
    with runtime_role():
        assert (
            client.post(
                URL, {"action": "cancel", "appointment_id": str(appointment.pk)}
            ).status_code
            == 303
        )
        assert (
            client.post(
                URL,
                {"action": "book", "slot": "invalid", "idempotency_key": str(uuid4())},
            ).status_code
            == 409
        )


def test_staff_patient_race_has_exactly_one_booking(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    with runtime_role(), patient_session_context(session_id):
        token = patient_slots(DAY)[2].token
    barrier = Barrier(2, timeout=10)

    def compete(*, patient: bool) -> UUID | str:
        connections.close_all()
        try:
            with runtime_role():
                barrier.wait()
                try:
                    if patient:
                        with patient_session_context(session_id):
                            return book_patient_slot(
                                token=token, idempotency_key=uuid4()
                            ).pk
                    with tenant_context(setup.actor_id, setup.organization_id):
                        return create_synthetic_appointment(
                            setup, end_local="2035-06-02T09:30"
                        ).pk
                except SlotConflict:
                    return "conflict"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, patient=patient) for patient in (True, False)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(isinstance(result, UUID) for result in results) == 1
    assert results.count("conflict") == 1
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Appointment.objects.count() == 1


def test_other_patient_rows_and_keys_are_opaque(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    key = uuid4()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        other = create_patient(
            clinic_id=setup.clinic_id,
            full_name="Private Other Patient",
            birth_date=date(1991, 1, 1),
            idempotency_key=uuid4(),
        )
        hidden = create_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=other.enrollment.pk,
            practitioner_id=setup.practitioner_id,
            local_range=AppointmentLocalRange("2035-06-02T09:00", "2035-06-02T09:30"),
            idempotency_key=key,
        )
    with runtime_role(), patient_session_context(session_id):
        slots = patient_slots(DAY)
        assert len(slots) == 7
        assert Appointment.objects.count() == 0
        for operation in (patient_appointment, cancel_patient_appointment):
            with pytest.raises(AppointmentAccessDeniedError):
                operation(hidden.pk)
        with pytest.raises(AppointmentAccessDeniedError):
            reschedule_patient_appointment(hidden.pk, slots[0].token)
        with pytest.raises(AppointmentIdempotencyConflictError):
            book_patient_slot(token=slots[0].token, idempotency_key=key)
        assert patient_appointments() == ()
    with runtime_role():
        response = _client(session_id).get(URL, {"day": DAY.isoformat()})
        assert response.status_code == 200
        assert str(hidden.pk).encode() not in response.content
        assert b"Private Other Patient" not in response.content


def test_patient_cannot_update_availability_or_forge_receipts(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    with runtime_role(), patient_session_context(session_id):
        slots = patient_slots(DAY)
        appointment = book_patient_slot(token=slots[0].token, idempotency_key=uuid4())
        assert AvailabilityBlock.objects.update(retired_at=appointment.created_at) == 0
        with pytest.raises(DatabaseError), transaction.atomic():
            PatientBookingEvent.objects.create(
                organization_id=setup.organization_id,
                clinic_id=setup.clinic_id,
                appointment_id=appointment.pk,
                patient_session_id=session_id,
                action="created",
                start_at=appointment.start_at,
                end_at=appointment.end_at,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            Appointment.objects.filter(pk=appointment.pk).update(
                status="cancelled",
                cancellation_reason="clinic_request",
                cancelled_at=appointment.created_at,
            )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        assert PatientBookingEvent.objects.update(action="forged") == 0
        assert list(PatientBookingEvent.objects.values_list("action", flat=True)) == [
            "created"
        ]


def test_patient_booking_catalog_is_narrow() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid='clinic_app.scheduling_patientbookingevent'::regclass"
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name='scheduling_patientbookingevent'"
        )
        assert cursor.fetchall() == [("SELECT",)]
        cursor.execute(
            "SELECT proname, proowner::regrole::text, prosecdef, proconfig, "
            "EXISTS (SELECT 1 FROM aclexplode(proacl) a "
            "WHERE a.grantee=0 AND a.privilege_type='EXECUTE') "
            "FROM pg_proc WHERE pronamespace='clinic_app'::regnamespace "
            "AND proname LIKE 'patient_booking_%'"
        )
        functions = cursor.fetchall()
    assert {row[0] for row in functions} == {
        "patient_booking_scope",
        "patient_booking_practitioner",
        "patient_booking_slots",
        "patient_booking_lock_availability",
        "patient_booking_guard",
        "patient_booking_receipt",
        "patient_booking_event_immutable",
    }
    assert all(
        row[1:]
        == (
            "clinic_resolver",
            True,
            ["search_path=pg_catalog, clinic_app, pg_temp"],
            False,
        )
        for row in functions
    )


@pytest.mark.parametrize(
    "action", ["unknown", "book", "reschedule", "cancel", "choose"]
)
def test_invalid_native_submission_is_recoverable(
    rbac_graph: RbacGraph, action: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    client = _client(_session(setup))
    with runtime_role():
        response = client.post(
            URL,
            {
                "action": action,
                "day": "invalid",
                "appointment_id": "invalid",
                "idempotency_key": "invalid",
            },
        )
        assert response.status_code == 409
        assert client.get(URL, {"day": "invalid"}).status_code == 409


def test_revoked_grant_denies_booking_even_if_session_row_is_not_revoked(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        PatientAccessGrant.objects.filter(enrollment_id=setup.enrollment_id).update(
            revoked_at=timezone.now()
        )
    with (
        runtime_role(),
        patient_session_context(session_id),
        pytest.raises(AppointmentAccessDeniedError),
    ):
        patient_slots(DAY)
    with runtime_role():
        assert _client(session_id).get(URL).status_code == 403


def test_physician_revocation_blocks_new_booking_but_preserves_equal_replay(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    key = uuid4()
    with runtime_role(), patient_session_context(session_id):
        slots = patient_slots(DAY)
        original = book_patient_slot(token=slots[0].token, idempotency_key=key)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        UserClinicRole.objects.filter(
            clinic_id=setup.clinic_id,
            user_id=setup.practitioner_id,
            role=UserClinicRole.Role.PHYSICIAN,
        ).delete()
    with runtime_role(), patient_session_context(session_id):
        assert patient_slots(DAY) == ()
        assert (
            book_patient_slot(token=slots[0].token, idempotency_key=key).pk
            == original.pk
        )
        with pytest.raises(AppointmentPractitionerError):
            book_patient_slot(token=slots[1].token, idempotency_key=uuid4())
