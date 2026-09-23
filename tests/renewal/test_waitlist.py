"""FIFO offers never reserve appointments; patient acceptance revalidates."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from threading import Barrier
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.intake.contacts import (
    save_contact_destination,
    set_purpose_channel,
    verify_contact,
)
from apps.intake.models import PatientSession
from apps.intake.patient_access import patient_session_context
from apps.intake.services import create_patient
from apps.scheduling import waitlist as waitlist_services
from apps.scheduling.models import (
    Appointment,
    AvailabilityBlock,
    PatientBookingEvent,
    WaitlistEntry,
    WaitlistOffer,
)
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentLocalRange,
    SlotConflict,
    cancel_appointment,
    create_appointment,
)
from apps.scheduling.waitlist import (
    WaitlistInputError,
    add_waitlist_entry,
    issue_waitlist_offer,
    patient_waitlist_offers,
    respond_to_offer,
    waitlist_notice_channels,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from django.test import Client
from django.utils import timezone

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from renewal.test_self_booking import _client, _session

if TYPE_CHECKING:
    from appointment_service_support import AppointmentSetup
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
URL = "/patient/offers/"


def _entry(setup: AppointmentSetup, **changes: str) -> WaitlistEntry:
    return add_waitlist_entry(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        practitioner_id=setup.practitioner_id,
        start_local=changes.get("start", "2035-06-02T08:00"),
        end_local=changes.get("end", "2035-06-02T12:00"),
    )


def _offer(setup: AppointmentSetup, **changes: str) -> WaitlistOffer | None:
    return issue_waitlist_offer(
        clinic_id=setup.clinic_id,
        practitioner_id=setup.practitioner_id,
        start_local=changes.get("start", "2035-06-02T09:00"),
        end_local=changes.get("end", "2035-06-02T09:30"),
    )


def _second(setup: AppointmentSetup) -> AppointmentSetup:
    registration = create_patient(
        clinic_id=setup.clinic_id,
        full_name="Synthetic Next Waitlist Patient",
        birth_date=date(1990, 1, 1),
        idempotency_key=uuid4(),
    )
    return replace(
        setup,
        enrollment_id=registration.enrollment.pk,
        patient_id=registration.patient.pk,
    )


def test_fifo_single_offer_acceptance_and_queue_advance(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        unmatched = _entry(setup, start="2035-06-02T10:00")
        first = _entry(setup)
        second = _entry(_second(setup))
        offer = _offer(setup)
        assert offer is not None
        assert offer.entry_id == first.pk
        assert offer.expires_at - offer.created_at == timedelta(minutes=30)
        repeated = _offer(setup)
        assert repeated is not None
        assert repeated.pk == offer.pk
        overlapping = _offer(setup, start="2035-06-02T09:15", end="2035-06-02T09:45")
        assert overlapping is not None
        assert overlapping.pk == offer.pk
        assert Appointment.objects.count() == 0
    with runtime_role(), patient_session_context(session):
        result = respond_to_offer(offer.pk, accept=True)
        assert result.state == "accepted"
        assert result.appointment_id is not None
        assert (
            respond_to_offer(offer.pk, accept=True).appointment_id
            == result.appointment_id
        )
        assert Appointment.objects.count() == 1
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        first.refresh_from_db()
        assert first.state == "fulfilled"
        assert WaitlistEntry.objects.get(pk=unmatched.pk).state == "waiting"
        next_offer = _offer(setup, start="2035-06-02T09:30", end="2035-06-02T10:00")
        assert next_offer is not None
        assert next_offer.entry_id == second.pk
        assert WaitlistOffer.objects.count() == 2


def test_expiry_is_terminal_and_advances_fifo_without_booking(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        first = _entry(setup)
        second = _entry(_second(setup))
        offer = _offer(setup)
        assert offer is not None
    # Owner setup simulates elapsed time, never waits on a wall-clock deadline.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(setup.organization_id)],
        )
        WaitlistOffer.objects.filter(pk=offer.pk).update(
            created_at=timezone.now() - timedelta(hours=1),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
    with runtime_role(), patient_session_context(session):
        assert respond_to_offer(offer.pk, accept=True).state == "expired"
        assert respond_to_offer(offer.pk, accept=True).state == "expired"
        assert Appointment.objects.count() == 0
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert WaitlistEntry.objects.get(pk=first.pk).state == "expired"
        next_offer = _offer(setup)
        assert next_offer is not None
        assert next_offer.entry_id == second.pk


def test_decline_history_and_stale_slot_recovery(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _entry(setup)
        offer = _offer(setup)
        assert offer is not None
    with runtime_role(), patient_session_context(session):
        assert respond_to_offer(offer.pk, accept=False).state == "declined"
        assert respond_to_offer(offer.pk, accept=True).state == "declined"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        entry = _entry(setup)
        stale = _offer(setup)
        assert stale is not None
        # An offer is not a reservation: ordinary staff booking succeeds.
        create_synthetic_appointment(_second(setup))
    with runtime_role(), patient_session_context(session):
        assert respond_to_offer(stale.pk, accept=True).state == "unavailable"
        assert Appointment.objects.count() == 0
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert WaitlistEntry.objects.get(pk=entry.pk).state == "waiting"
        assert WaitlistOffer.objects.count() == 2
        assert Appointment.objects.count() == 1


def test_scope_denial_and_patient_http_recovery(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        other = _second(setup)
        _entry(other)
        offer = _offer(setup)
        assert offer is not None
        with pytest.raises(AppointmentAccessDeniedError):
            add_waitlist_entry(
                clinic_id=rbac_graph.clinic_b,
                enrollment_id=setup.enrollment_id,
                practitioner_id=setup.practitioner_id,
                start_local="2035-06-02T08:00",
                end_local="2035-06-02T12:00",
            )
    with runtime_role(), patient_session_context(session):
        assert patient_waitlist_offers() == ()
        assert WaitlistOffer.objects.count() == 0
        assert WaitlistEntry.objects.count() == 0
        with pytest.raises(AppointmentAccessDeniedError):
            respond_to_offer(offer.pk, accept=True)
        with pytest.raises(AppointmentAccessDeniedError):
            _entry(setup)
    with runtime_role():
        client = _client(session)
        assert client.get(URL).status_code == 200
        assert (
            client.post(
                URL, {"action": "accept", "offer_id": str(offer.pk)}
            ).status_code
            == 403
        )
        assert (
            client.post(
                URL,
                {
                    "action": "accept",
                    "offer_id": str(uuid4()),
                    "patient_id": str(other.patient_id),
                },
            ).status_code
            == 403
        )
        assert (
            client.post(URL, {"action": "accept", "offer_id": "invalid"}).status_code
            == 409
        )


def test_notice_requires_separate_opt_in_verified_current_contact(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _entry(setup)
        offer = _offer(setup)
        assert offer is not None
        assert waitlist_notice_channels(offer.pk) == ()
        contact = save_contact_destination(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            channel="email",
            destination="synthetic@example.test",
            expected_version=None,
        )
        verify_contact(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            channel="email",
            expected_version=contact.destination_version,
        )
        set_purpose_channel(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            purpose="booking_confirmation",
            channel="email",
        )
        assert waitlist_notice_channels(offer.pk) == ()
        set_purpose_channel(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            purpose="waitlist_offer",
            channel="email",
        )
        assert waitlist_notice_channels(offer.pk) == ("email",)
        changed = save_contact_destination(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            channel="email",
            destination="changed@example.test",
            expected_version=contact.destination_version,
        )
        assert waitlist_notice_channels(offer.pk) == ()
        verify_contact(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            channel="email",
            expected_version=changed.destination_version,
        )
        assert waitlist_notice_channels(offer.pk) == ("email",)
        set_purpose_channel(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            purpose="waitlist_offer",
            channel=None,
        )
        assert waitlist_notice_channels(offer.pk) == ()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2035-06-02T12:00", "2035-06-02T08:00"),
        ("invalid", "2035-06-02T12:00"),
        ("2020-01-01T08:00", "2020-01-01T12:00"),
    ],
)
def test_bad_windows_rejected(rbac_graph: RbacGraph, start: str, end: str) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(WaitlistInputError):
            _entry(setup, start=start, end=end)
        assert WaitlistEntry.objects.count() == 0


def test_concurrent_issuance_produces_one_offer(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        entry = _entry(setup)
        _entry(_second(setup))
    barrier = Barrier(2, timeout=10)

    def issue() -> UUID:
        connections.close_all()
        try:
            with runtime_role():
                barrier.wait()
                with tenant_context(setup.actor_id, setup.organization_id):
                    offer = _offer(setup)
                    assert offer is not None
                    return offer.pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(issue) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert WaitlistOffer.objects.get().entry_id == entry.pk


def test_staff_booking_races_acceptance_without_double_booking(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _entry(setup)
        other = _second(setup)
        offer = _offer(setup)
        assert offer is not None
    barrier = Barrier(2, timeout=10)

    def compete(*, patient: bool) -> str:
        connections.close_all()
        try:
            with runtime_role():
                barrier.wait()
                if patient:
                    with patient_session_context(session):
                        return respond_to_offer(offer.pk, accept=True).state
                with tenant_context(setup.actor_id, setup.organization_id):
                    try:
                        create_synthetic_appointment(
                            other, end_local="2035-06-02T09:30"
                        )
                    except SlotConflict:
                        return "conflict"
                    return "booked"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, patient=patient) for patient in (True, False)]
        results = [future.result(timeout=20) for future in futures]
    assert results in (["accepted", "conflict"], ["unavailable", "booked"])
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Appointment.objects.count() == 1


def test_expiry_during_booking_rolls_back_appointment_and_receipt(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        entry = _entry(setup)
        offer = _offer(setup)
        assert offer is not None

    def elapsed_booking(
        *,
        clinic_id: UUID,
        enrollment_id: UUID,
        practitioner_id: UUID,
        local_range: AppointmentLocalRange,
        idempotency_key: UUID,
    ) -> Appointment:
        appointment = create_appointment(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            practitioner_id=practitioner_id,
            local_range=local_range,
            idempotency_key=idempotency_key,
        )
        monkeypatch.setattr(
            waitlist_services, "timezone", SimpleNamespace(now=lambda: offer.expires_at)
        )
        return appointment

    monkeypatch.setattr(waitlist_services, "create_appointment", elapsed_booking)
    with runtime_role(), patient_session_context(session):
        assert respond_to_offer(offer.pk, accept=True).state == "expired"
        assert Appointment.objects.count() == 0
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert PatientBookingEvent.objects.count() == 0
        assert WaitlistEntry.objects.get(pk=entry.pk).state == "expired"


@pytest.mark.parametrize("cancelled", [False, True])
def test_offer_key_collision_never_fabricates_confirmation(
    rbac_graph: RbacGraph,
    cancelled: bool,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _entry(setup)
        offer = _offer(setup)
        assert offer is not None
        appointment = create_synthetic_appointment(
            setup,
            idempotency_key=offer.pk,
            start_local="2035-06-02T09:00" if cancelled else "2035-06-02T10:00",
            end_local="2035-06-02T09:30" if cancelled else "2035-06-02T10:30",
        )
        if cancelled:
            cancel_appointment(appointment_id=appointment.pk, reason="patient_request")
    with runtime_role(), patient_session_context(session):
        result = respond_to_offer(offer.pk, accept=True)
        assert result.state == "unavailable"
        assert result.appointment_id is None
        assert Appointment.objects.count() == 1


def test_empty_ineligible_and_retired_openings(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert _offer(setup) is None
        first = _entry(setup)
        assert _entry(setup).pk == first.pk
        with pytest.raises(WaitlistInputError):
            _offer(setup, end="2035-06-03T09:30")
        with pytest.raises(AppointmentAccessDeniedError):
            _entry(replace(setup, enrollment_id=uuid4()))
        existing = create_synthetic_appointment(setup)
        with pytest.raises(WaitlistInputError):
            _offer(setup)
        offer = _offer(setup, start="2035-06-02T10:00", end="2035-06-02T10:30")
        assert offer is not None
        cancel_appointment(appointment_id=existing.pk, reason="patient_request")
    # Owner fixture invalidates the availability after the offer was issued.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(setup.organization_id)],
        )
        AvailabilityBlock.objects.filter(clinic_id=setup.clinic_id).update(
            retired_at=timezone.now()
        )
    session = _session(setup)
    with runtime_role(), patient_session_context(session):
        assert respond_to_offer(offer.pk, accept=True).state == "unavailable"


def test_revoked_operation_and_other_clinic_role_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _entry(setup)
        offer = _offer(setup)
        assert offer is not None
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(setup.organization_id)],
        )
        PatientSession.objects.filter(pk=session).update(operations=["enrollment_view"])
    with runtime_role(), patient_session_context(session):
        assert WaitlistOffer.objects.count() == 0
        with pytest.raises(AppointmentAccessDeniedError):
            respond_to_offer(offer.pk, accept=True)
    with runtime_role(), tenant_context(rbac_graph.physician, setup.organization_id):
        assert WaitlistOffer.objects.count() == 0
        with pytest.raises(AppointmentAccessDeniedError):
            _offer(setup)
    with runtime_role():
        assert Client().get(URL).status_code == 403
        assert _client(session).get(URL).status_code == 403


def test_native_staff_and_patient_post_states(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    client, _ = receptionist_client(rbac_graph)
    queue = f"/scheduling/clinics/{setup.clinic_id}/waitlist/"
    entry_body = {
        "action": "add",
        "entry-enrollment": str(setup.enrollment_id),
        "entry-practitioner": str(setup.practitioner_id),
        "entry-start_local": "2035-06-02T08:00",
        "entry-end_local": "2035-06-02T12:00",
    }
    offer_body = {
        "action": "offer",
        "offer-practitioner": str(setup.practitioner_id),
        "offer-start_local": "2035-06-02T09:00",
        "offer-end_local": "2035-06-02T09:30",
    }
    with runtime_role():
        assert client.get(queue).status_code == 200
        assert client.post(queue, {"action": "bad"}).status_code == 409
        assert (
            client.post(queue, {**entry_body, "entry-end_local": "invalid"}).status_code
            == 409
        )
        assert client.post(queue, entry_body).status_code == 303
        assert client.post(queue, offer_body).status_code == 303
        assert client.get(queue).status_code == 200
        assert (
            client.get(
                f"/scheduling/clinics/{rbac_graph.clinic_b}/waitlist/"
            ).status_code
            == 404
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        offer = WaitlistOffer.objects.get()
    session = _session(setup)
    patient = _client(session)
    with runtime_role():
        assert patient.get(URL).status_code == 200
        assert patient.post(URL, {"action": "invalid"}).status_code == 409
        assert (
            patient.post(
                URL, {"action": "accept", "offer_id": str(offer.pk)}
            ).status_code
            == 303
        )
        assert patient.get(URL).status_code == 200
        assert client.post(queue, offer_body).status_code == 409


def test_exact_waitlist_database_posture_and_immutable_history(
    rbac_graph: RbacGraph,
) -> None:
    tables = ["scheduling_waitlistentry", "scheduling_waitlistoffer"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relnamespace='clinic_app'::regnamespace "
            "AND relname=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {(table, True, True) for table in tables}
        cursor.execute(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (table, policy)
            for table in tables
            for policy in (
                "setup_tenant",
                "waitlist_read",
                "waitlist_insert",
                "waitlist_update",
            )
        }
        cursor.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (table, privilege) for table in tables for privilege in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT table_name, column_name "
            "FROM information_schema.role_column_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name=ANY(%s) AND privilege_type='UPDATE'",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            ("scheduling_waitlistentry", "state"),
            ("scheduling_waitlistoffer", "state"),
            ("scheduling_waitlistoffer", "responded_at"),
            ("scheduling_waitlistoffer", "appointment_id"),
        }
        cursor.execute(
            "SELECT proname, prosecdef, proconfig, "
            "EXISTS (SELECT 1 FROM aclexplode(proacl) a "
            "WHERE a.grantee=0 AND a.privilege_type='EXECUTE') "
            "FROM pg_proc WHERE pronamespace='clinic_app'::regnamespace "
            "AND proname IN ('waitlist_staff','waitlist_binding')"
        )
        assert {
            (name, definer, tuple(config), public)
            for name, definer, config, public in cursor.fetchall()
        } == {
            (name, True, ("search_path=pg_catalog, clinic_app, pg_temp",), False)
            for name in ("waitlist_staff", "waitlist_binding")
        }
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _entry(setup)
        offer = _offer(setup)
        assert offer is not None
    with runtime_role(), patient_session_context(session):
        respond_to_offer(offer.pk, accept=False)
        with pytest.raises(DatabaseError), transaction.atomic():
            WaitlistOffer.objects.filter(pk=offer.pk).update(state="pending")
        with pytest.raises(DatabaseError), transaction.atomic():
            WaitlistOffer.objects.filter(pk=offer.pk).update(expires_at=timezone.now())
        with pytest.raises(DatabaseError), transaction.atomic():
            WaitlistOffer.objects.filter(pk=offer.pk).delete()


@pytest.mark.parametrize("surface", ["staff", "patient"])
def test_lazy_expiry_preserves_history_and_http_recovery(
    rbac_graph: RbacGraph,
    surface: str,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _entry(setup)
        next_entry = _entry(_second(setup))
        offer = _offer(setup)
        assert offer is not None
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(setup.organization_id)],
        )
        WaitlistOffer.objects.filter(pk=offer.pk).update(
            created_at=timezone.now() - timedelta(hours=1),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
    if surface == "staff":
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            new = _offer(setup)
            assert new is not None
            assert new.entry_id == next_entry.pk
    else:
        with runtime_role(), patient_session_context(session):
            assert patient_waitlist_offers()[0].state == "expired"
    with runtime_role():
        response = _client(session).post(
            URL, {"action": "accept", "offer_id": str(offer.pk)}
        )
        assert response.status_code == 409
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert WaitlistOffer.objects.get(pk=offer.pk).state == "expired"
        assert waitlist_notice_channels(offer.pk) == ()
        assert Appointment.objects.count() == 0
