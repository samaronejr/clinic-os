"""Acceptance tests for verified contacts and per-channel preferences.

Covers the intake contact/preference services, the send-time recheck wired
into the task-13 boundary, and the POST-only staff screens. Destinations
are synthetic; verification is the explicit staff-attested method.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from threading import Event
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.adapters import OperationScope, SendAdapter, SendResult
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.core import integration
from apps.core.integration import (
    OperationRequest,
    clear_integration_registrations,
    enqueue_operation,
    register_send_adapter,
)
from apps.intake.contacts import (
    SUBJECT_TYPE_PREFERENCE,
    preference_send_eligible,
)
from apps.intake.models import (
    PatientChannelPreference,
    PatientContact,
    PatientContactEvent,
)
from apps.intake.services import (
    AutomatedMessageError,
    ContactConflictError,
    ContactInputError,
    PatientAccessDeniedError,
    contact_for_edit,
    contact_overview,
    create_patient,
    enqueue_automated_message,
    mask_destination,
    save_contact_destination,
    set_purpose_channel,
    verify_contact,
)
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client
from django.utils.translation import gettext

from otp_test_support import OTP_RAW_CREDENTIAL, create_receptionist, runtime_role
from patient_service_support import runtime_role as service_runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from contextlib import AbstractContextManager

    from django.db.models.base import ModelBase

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

PROVIDER: Final = "synthetic-contacts-provider"
SMS: Final = PatientContact.Channel.SMS
EMAIL: Final = PatientContact.Channel.EMAIL
WHATSAPP: Final = PatientContact.Channel.WHATSAPP
REMINDER: Final = PatientChannelPreference.Purpose.APPOINTMENT_REMINDER
CONFIRMATION: Final = PatientChannelPreference.Purpose.BOOKING_CONFIRMATION
PENDING: Final = IntegrationOperation.Status.PENDING
CANCELLED: Final = IntegrationOperation.Status.CANCELLED
SUCCEEDED: Final = IntegrationOperation.Status.SUCCEEDED


@dataclass
class RecordingAdapter:
    """Synthetic provider adapter recording each external send."""

    provider: str = PROVIDER
    sent: list[tuple[UUID, object]] = field(default_factory=list)

    def prepare(self, operation: IntegrationOperation) -> object:
        """Return the minimal subject reference for the send."""
        return {
            "subject_type": operation.subject_type,
            "subject_id": str(operation.subject_id),
        }

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Record the external call and accept it."""
        self.sent.append((operation_id, prepared))
        return SendResult(provider_reference=f"ref-{uuid4().hex[:12]}")


@pytest.fixture
def adapter() -> Iterator[RecordingAdapter]:
    """Register the synthetic provider for one test."""
    recording = RecordingAdapter()
    register_send_adapter(recording)
    try:
        yield recording
    finally:
        clear_integration_registrations()


@pytest.fixture
def recorded_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record dispatch attempts without executing the task body."""
    dispatched: list[str] = []
    monkeypatch.setattr(
        execute_operation_task,
        "apply_async",
        lambda **kwargs: dispatched.append(kwargs["kwargs"]["operation_id"]),
    )
    return dispatched


@dataclass(frozen=True, slots=True)
class ContactGraph:
    """One enrolled patient plus a second enrolled patient in clinic A."""

    enrollment_id: UUID
    patient_id: UUID
    other_enrollment_id: UUID
    other_patient_id: UUID


def _seed_patients(graph: RbacGraph) -> ContactGraph:
    with (
        service_runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
    ):
        first = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Ana Synthetic Contact",
            birth_date=date(1990, 5, 17),
            idempotency_key=uuid4(),
        )
        second = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Bia Synthetic Contact",
            birth_date=date(1985, 3, 9),
            idempotency_key=uuid4(),
        )
    return ContactGraph(
        enrollment_id=first.enrollment.pk,
        patient_id=first.patient.pk,
        other_enrollment_id=second.enrollment.pk,
        other_patient_id=second.patient.pk,
    )


def _as_manager(graph: RbacGraph) -> AbstractContextManager[None]:
    return tenant_context(graph.shared_user, graph.organization_a)


def _save(
    graph: RbacGraph,
    enrollment_id: UUID,
    *,
    channel: str = SMS,
    destination: str = "+5511987654321",
    expected_version: int | None = None,
) -> PatientContact:
    with service_runtime_role(), _as_manager(graph):
        return save_contact_destination(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment_id,
            channel=channel,
            destination=destination,
            expected_version=expected_version,
        )


def _verify(
    graph: RbacGraph,
    enrollment_id: UUID,
    *,
    channel: str = SMS,
    expected_version: int = 1,
) -> PatientContact:
    with service_runtime_role(), _as_manager(graph):
        return verify_contact(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment_id,
            channel=channel,
            expected_version=expected_version,
        )


def _choose(
    graph: RbacGraph,
    enrollment_id: UUID,
    *,
    purpose: str = REMINDER,
    channel: str | None = SMS,
) -> tuple[PatientChannelPreference, ...]:
    with service_runtime_role(), _as_manager(graph):
        return set_purpose_channel(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment_id,
            purpose=purpose,
            channel=channel,
        )


def _enqueue(
    graph: RbacGraph,
    enrollment_id: UUID,
    *,
    purpose: str = REMINDER,
) -> UUID:
    with service_runtime_role(), _as_manager(graph):
        return enqueue_automated_message(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment_id,
            purpose=purpose,
            provider=PROVIDER,
            idempotency_key=uuid4(),
        )


def _run_task(operation_id: UUID) -> str:
    result = execute_operation_task.apply(kwargs={"operation_id": str(operation_id)})
    return str(result.result)


def _operation(organization_id: UUID, operation_id: UUID) -> IntegrationOperation:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        operation = IntegrationOperation.objects.filter(pk=operation_id).first()
    assert operation is not None
    return operation


def _events(
    graph: RbacGraph,
    patient_id: UUID,
) -> list[str]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        rows = list(
            PatientContactEvent.objects.filter(
                patient_id=patient_id,
                clinic_id=graph.clinic_a,
            ).values_list("event_type", "version")
        )
    return sorted(f"{event_type}:{version}" for event_type, version in rows)


def _audit_verbs(organization_id: UUID, record_id: UUID) -> list[str]:
    rows = AuditEvent.objects.filter(
        organization_id=organization_id,
        affected_record_id=str(record_id),
    ).values_list("payload__object_verb", flat=True)
    return sorted(rows)


def test_destination_lifecycle_versions_verification_and_history(
    rbac_graph: RbacGraph,
) -> None:
    contacts = _seed_patients(rbac_graph)

    contact = _save(rbac_graph, contacts.enrollment_id)
    assert contact.destination_version == 1
    assert contact.verified_version == 0
    assert not contact.is_verified

    verified = _verify(rbac_graph, contacts.enrollment_id)
    assert verified.is_verified
    assert verified.verified_version == 1
    assert verified.verification_method == "staff_attested"
    assert verified.verified_at is not None

    # Saving the same destination is a no-op; changing it bumps the
    # destination version and invalidates the prior verification.
    unchanged = _save(
        rbac_graph,
        contacts.enrollment_id,
        expected_version=1,
    )
    assert unchanged.destination_version == 1
    assert unchanged.is_verified
    changed = _save(
        rbac_graph,
        contacts.enrollment_id,
        destination="+5511911112222",
        expected_version=1,
    )
    assert changed.destination_version == 2
    assert changed.verified_version == 0
    assert changed.verified_at is None
    assert changed.verification_method == ""
    assert not changed.is_verified

    assert _events(rbac_graph, contacts.patient_id) == [
        "contact_saved:1",
        "contact_saved:2",
        "contact_verified:1",
        "verification_invalidated:2",
    ]
    assert _audit_verbs(rbac_graph.organization_a, contact.pk) == [
        "saved",
        "saved",
        "verified",
    ]


def test_stale_edit_version_conflicts_instead_of_overwriting(
    rbac_graph: RbacGraph,
) -> None:
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)

    with service_runtime_role(), _as_manager(graph := rbac_graph):
        with pytest.raises(ContactConflictError):
            save_contact_destination(
                clinic_id=graph.clinic_a,
                enrollment_id=contacts.enrollment_id,
                channel=SMS,
                destination="+5511900000000",
                expected_version=7,
            )
        # A form rendered before the contact existed cannot create over it.
        with pytest.raises(ContactConflictError):
            save_contact_destination(
                clinic_id=graph.clinic_a,
                enrollment_id=contacts.enrollment_id,
                channel=SMS,
                destination="+5511900000000",
                expected_version=None,
            )
        with pytest.raises(ContactInputError):
            save_contact_destination(
                clinic_id=graph.clinic_a,
                enrollment_id=contacts.enrollment_id,
                channel=EMAIL,
                destination="not-an-email",
                expected_version=None,
            )
        with pytest.raises(ContactInputError):
            save_contact_destination(
                clinic_id=graph.clinic_a,
                enrollment_id=contacts.enrollment_id,
                channel=SMS,
                destination="123",
                expected_version=None,
            )


def test_preferences_are_versioned_revocable_and_clinic_scoped(
    rbac_graph: RbacGraph,
) -> None:
    contacts = _seed_patients(rbac_graph)

    changed = _choose(rbac_graph, contacts.enrollment_id, channel=SMS)
    assert len(changed) == 1
    preference = changed[0]
    assert preference.opted_in
    assert preference.version == 1

    # Choosing another channel moves the opt-in and bumps both versions.
    changed = _choose(rbac_graph, contacts.enrollment_id, channel=EMAIL)
    assert len(changed) == 2
    states = {item.channel: (item.opted_in, item.version) for item in changed}
    assert states == {EMAIL: (True, 1), SMS: (False, 2)}

    # Re-choosing the same channel is a no-op; revoking writes history.
    assert _choose(rbac_graph, contacts.enrollment_id, channel=EMAIL) == ()
    revoked = _choose(rbac_graph, contacts.enrollment_id, channel=None)
    assert len(revoked) == 1
    assert revoked[0].channel == EMAIL
    assert not revoked[0].opted_in
    assert revoked[0].version == 2

    assert _events(rbac_graph, contacts.patient_id) == [
        "preference_opted_in:1",
        "preference_opted_in:1",
        "preference_opted_out:2",
        "preference_opted_out:2",
    ]

    # Preferences live in clinic A: a clinic-B manager sees the same
    # patient's clinic-B enrollment with no opt-in anywhere.
    with (
        service_runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        overview_b = contact_overview(
            clinic_id=rbac_graph.clinic_b,
            enrollment_id=_enroll_in_clinic_b(rbac_graph, contacts.patient_id),
        )
    assert overview_b.preferences == ()


def _enroll_in_clinic_b(graph: RbacGraph, patient_id: UUID) -> UUID:
    """Enroll one existing patient in clinic B through the service path."""
    from apps.intake.models import PatientClinicEnrollment  # noqa: PLC0415

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            patient_id=patient_id,
            idempotency_key=uuid4(),
            create_fingerprint=b"b" * 32,
        )
    return enrollment.pk


def test_masked_overview_and_full_edit_destination(
    rbac_graph: RbacGraph,
) -> None:
    contacts = _seed_patients(rbac_graph)
    _save(
        rbac_graph,
        contacts.enrollment_id,
        channel=EMAIL,
        destination="marina.sintetica@example.com",
    )

    with service_runtime_role(), _as_manager(rbac_graph):
        overview = contact_overview(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=contacts.enrollment_id,
        )
        target = contact_for_edit(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=contacts.enrollment_id,
            channel=EMAIL,
        )

    assert len(overview.contacts) == 1
    view = overview.contacts[0]
    assert view.masked_destination == "m•••@e•••.com"
    assert "marina.sintetica" not in view.masked_destination
    assert not view.verified
    assert target.destination == "marina.sintetica@example.com"
    assert target.destination_version == 1

    assert mask_destination(SMS, "+55 11 98765-4321") == "••• •••• 4321"
    assert mask_destination(WHATSAPP, "123") == "••••"
    assert mask_destination(EMAIL, "a@b") == "a•••@b•••"


def test_contact_history_is_append_only(rbac_graph: RbacGraph) -> None:
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        event = PatientContactEvent.objects.filter(
            patient_id=contacts.patient_id
        ).first()
        assert event is not None
        event.version = 99
        with pytest.raises(IntegrityError):
            event.save(update_fields=("version",))
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        with pytest.raises(IntegrityError):
            PatientContactEvent.objects.filter(patient_id=contacts.patient_id).delete()


def test_send_time_recheck_cancels_after_destination_change(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
) -> None:
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)
    _verify(rbac_graph, contacts.enrollment_id)
    _choose(rbac_graph, contacts.enrollment_id, channel=SMS)
    operation_id = _enqueue(rbac_graph, contacts.enrollment_id)
    assert recorded_dispatch == [str(operation_id)]

    # The destination changes while the send is queued.
    _save(
        rbac_graph,
        contacts.enrollment_id,
        destination="+5511966667777",
        expected_version=1,
    )

    with service_runtime_role():
        assert _run_task(operation_id) == "cancelled"

    assert adapter.sent == []
    operation = _operation(rbac_graph.organization_a, operation_id)
    assert operation.status == CANCELLED
    assert operation.last_error == "subject_ineligible"
    assert _audit_verbs(rbac_graph.organization_a, operation_id) == [
        "cancelled",
        "enqueued",
    ]


def test_send_time_recheck_cancels_after_opt_out(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
) -> None:
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)
    _verify(rbac_graph, contacts.enrollment_id)
    _choose(rbac_graph, contacts.enrollment_id, channel=SMS)
    operation_id = _enqueue(rbac_graph, contacts.enrollment_id)
    assert recorded_dispatch == [str(operation_id)]

    _choose(rbac_graph, contacts.enrollment_id, channel=None)

    with service_runtime_role():
        assert _run_task(operation_id) == "cancelled"

    assert adapter.sent == []
    operation = _operation(rbac_graph.organization_a, operation_id)
    assert operation.status == CANCELLED
    assert operation.last_error == "subject_ineligible"


def test_unverified_or_opted_out_destinations_never_send(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
) -> None:
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)
    _choose(rbac_graph, contacts.enrollment_id, channel=SMS)

    # Opted in but unverified: enqueue refuses before any operation exists.
    with (
        service_runtime_role(),
        _as_manager(rbac_graph),
        pytest.raises(AutomatedMessageError),
    ):
        enqueue_automated_message(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=contacts.enrollment_id,
            purpose=REMINDER,
            provider=PROVIDER,
            idempotency_key=uuid4(),
        )
    assert recorded_dispatch == []

    # Verified but not opted in for this purpose.
    _verify(rbac_graph, contacts.enrollment_id)
    with (
        service_runtime_role(),
        _as_manager(rbac_graph),
        pytest.raises(AutomatedMessageError),
    ):
        enqueue_automated_message(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=contacts.enrollment_id,
            purpose=CONFIRMATION,
            provider=PROVIDER,
            idempotency_key=uuid4(),
        )
    assert recorded_dispatch == []

    # A directly enqueued operation still rechecks at send time.
    with service_runtime_role(), _as_manager(rbac_graph):
        operation_id = enqueue_operation(
            OperationRequest(
                channel=SMS,
                provider=PROVIDER,
                clinic_id=rbac_graph.clinic_a,
                subject_type=SUBJECT_TYPE_PREFERENCE,
                subject_id=uuid4(),
                idempotency_key=uuid4(),
            )
        )
    with service_runtime_role():
        assert _run_task(operation_id) == "cancelled"
    assert adapter.sent == []
    assert _operation(rbac_graph.organization_a, operation_id).last_error == (
        "subject_ineligible"
    )


def test_verified_opted_in_destination_sends_once(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
) -> None:
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)
    _verify(rbac_graph, contacts.enrollment_id)
    (preference,) = _choose(rbac_graph, contacts.enrollment_id, channel=SMS)
    operation_id = _enqueue(rbac_graph, contacts.enrollment_id)
    assert recorded_dispatch == [str(operation_id)]

    with service_runtime_role():
        assert _run_task(operation_id) == "succeeded"
        assert _run_task(operation_id) == "skipped"

    assert len(adapter.sent) == 1
    _, prepared = adapter.sent[0]
    assert prepared == {
        "subject_type": SUBJECT_TYPE_PREFERENCE,
        "subject_id": str(preference.pk),
    }
    operation = _operation(rbac_graph.organization_a, operation_id)
    assert operation.status == SUCCEEDED
    assert operation.channel == SMS


def test_verification_never_crosses_patients_or_clinics(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
) -> None:
    contacts = _seed_patients(rbac_graph)
    identical = "+5511987654321"
    _save(rbac_graph, contacts.enrollment_id, destination=identical)
    _save(rbac_graph, contacts.other_enrollment_id, destination=identical)
    _verify(rbac_graph, contacts.enrollment_id)
    _choose(rbac_graph, contacts.enrollment_id, channel=SMS)
    _choose(rbac_graph, contacts.other_enrollment_id, channel=SMS)

    # Verifying patient A's destination never verifies patient B's identical
    # one: B's enqueue refuses and B's queued operation cancels at send time.
    with service_runtime_role(), _as_manager(rbac_graph):
        with pytest.raises(AutomatedMessageError):
            enqueue_automated_message(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=contacts.other_enrollment_id,
                purpose=REMINDER,
                provider=PROVIDER,
                idempotency_key=uuid4(),
            )
        preference_b = PatientChannelPreference.objects.get(
            patient_id=contacts.other_patient_id,
            clinic_id=rbac_graph.clinic_a,
            purpose=REMINDER,
            channel=SMS,
        )
        operation_b = enqueue_operation(
            OperationRequest(
                channel=SMS,
                provider=PROVIDER,
                clinic_id=rbac_graph.clinic_a,
                subject_type=SUBJECT_TYPE_PREFERENCE,
                subject_id=preference_b.pk,
                idempotency_key=uuid4(),
            )
        )
    with service_runtime_role():
        assert _run_task(operation_b) == "cancelled"
    assert adapter.sent == []

    # A foreign-tenant preference id can never resolve inside tenant A.
    with service_runtime_role(), _as_manager(rbac_graph):
        foreign = enqueue_operation(
            OperationRequest(
                channel=SMS,
                provider=PROVIDER,
                clinic_id=rbac_graph.clinic_a,
                subject_type=SUBJECT_TYPE_PREFERENCE,
                subject_id=uuid4(),
                idempotency_key=uuid4(),
            )
        )
    with service_runtime_role():
        assert _run_task(foreign) == "cancelled"
    assert adapter.sent == []


def test_contact_services_deny_foreign_and_unassigned_clinics(
    rbac_graph: RbacGraph,
) -> None:
    contacts = _seed_patients(rbac_graph)

    denied = (
        (rbac_graph.physician, rbac_graph.clinic_a),
        (rbac_graph.shared_user, rbac_graph.clinic_b),
        (rbac_graph.shared_user, rbac_graph.clinic_c),
    )
    for actor_id, clinic_id in denied:
        with (
            service_runtime_role(),
            tenant_context(actor_id, rbac_graph.organization_a),
        ):
            with pytest.raises(PatientAccessDeniedError):
                contact_overview(
                    clinic_id=clinic_id,
                    enrollment_id=contacts.enrollment_id,
                )
            with pytest.raises(PatientAccessDeniedError):
                save_contact_destination(
                    clinic_id=clinic_id,
                    enrollment_id=contacts.enrollment_id,
                    channel=SMS,
                    destination="+5511987654321",
                    expected_version=None,
                )
            with pytest.raises(PatientAccessDeniedError):
                verify_contact(
                    clinic_id=clinic_id,
                    enrollment_id=contacts.enrollment_id,
                    channel=SMS,
                    expected_version=1,
                )
            with pytest.raises(PatientAccessDeniedError):
                set_purpose_channel(
                    clinic_id=clinic_id,
                    enrollment_id=contacts.enrollment_id,
                    purpose=REMINDER,
                    channel=SMS,
                )

    # A valid manager cannot reach an enrollment outside the clinic.
    with (
        service_runtime_role(),
        _as_manager(rbac_graph),
        pytest.raises(PatientAccessDeniedError),
    ):
        contact_overview(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=uuid4(),
        )


def test_contacts_http_flow_masks_edits_verifies_and_revokes(
    rbac_graph: RbacGraph,
) -> None:
    contacts = _seed_patients(rbac_graph)
    receptionist = create_receptionist(rbac_graph)
    client = Client()
    with runtime_role():
        assert client.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )
    url = f"/intake/clinics/{rbac_graph.clinic_a}/contacts/"

    with runtime_role():
        blank = client.get(url)
        assert blank.status_code == 200
        assert gettext("Contacts and messaging preferences").encode() in blank.content

        manage = client.post(
            url,
            {"action": "manage", "enrollment_id": str(contacts.enrollment_id)},
        )
        assert manage.status_code == 200
        assert b"Ana Synthetic Contact" in manage.content
        assert gettext("No destination recorded").encode() in manage.content

        edit = client.post(
            url,
            {
                "action": "edit",
                "enrollment_id": str(contacts.enrollment_id),
                "channel": SMS,
            },
        )
        assert edit.status_code == 200

        saved = client.post(
            url,
            {
                "action": "save",
                "enrollment_id": str(contacts.enrollment_id),
                "channel": SMS,
                "destination": "+55 11 98765-4321",
                "expected_version": "0",
            },
        )
        assert saved.status_code == 200
        # The manage screen masks; the full destination never renders there.
        assert b"+55 11 98765-4321" not in saved.content
        assert b"98765" not in saved.content
        assert "••• •••• 4321".encode() in saved.content
        assert gettext("Not verified").encode() in saved.content

        verified = client.post(
            url,
            {
                "action": "verify",
                "enrollment_id": str(contacts.enrollment_id),
                "channel": SMS,
                "expected_version": "1",
            },
        )
        assert verified.status_code == 200
        assert gettext("Verified").encode() in verified.content

        chosen = client.post(
            url,
            {
                "action": "preference",
                "enrollment_id": str(contacts.enrollment_id),
                "purpose": REMINDER,
                "channel": SMS,
            },
        )
        assert chosen.status_code == 200

        revoked = client.post(
            url,
            {
                "action": "preference",
                "enrollment_id": str(contacts.enrollment_id),
                "purpose": REMINDER,
                "channel": "",
            },
        )
        assert revoked.status_code == 200
        assert gettext("Messages revoked").encode() in revoked.content

    assert _events(rbac_graph, contacts.patient_id) == [
        "contact_saved:1",
        "contact_verified:1",
        "preference_opted_in:1",
        "preference_opted_out:2",
    ]


def test_contacts_http_denies_foreign_clinic_and_bad_actions(
    rbac_graph: RbacGraph,
) -> None:
    contacts = _seed_patients(rbac_graph)
    receptionist = create_receptionist(rbac_graph)
    client = Client()
    with runtime_role():
        assert client.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )

    foreign = f"/intake/clinics/{rbac_graph.clinic_c}/contacts/"
    clinic_a = f"/intake/clinics/{rbac_graph.clinic_a}/contacts/"
    with runtime_role():
        # Another organization's clinic: denied on GET and every action.
        assert client.get(foreign).status_code == 404
        for action in ("manage", "edit", "save", "verify", "preference"):
            assert (
                client.post(
                    foreign,
                    {
                        "action": action,
                        "enrollment_id": str(contacts.enrollment_id),
                        "channel": SMS,
                        "purpose": REMINDER,
                        "destination": "+5511987654321",
                        "expected_version": "0",
                    },
                ).status_code
                == 404
            )
        # A clinic the actor holds no role in, same organization.
        clinic_b = f"/intake/clinics/{rbac_graph.clinic_b}/contacts/"
        assert client.get(clinic_b).status_code == 404
        assert (
            client.post(
                clinic_b,
                {"action": "manage", "enrollment_id": str(contacts.enrollment_id)},
            ).status_code
            == 404
        )
        # Unknown actions and malformed bodies fail closed.
        assert (
            client.post(
                clinic_a,
                {"action": "bogus", "enrollment_id": str(contacts.enrollment_id)},
            ).status_code
            == 404
        )
        assert client.post(clinic_a, {"action": "manage"}).status_code == 404
        assert (
            client.post(
                clinic_a,
                {"action": "manage", "enrollment_id": str(uuid4())},
            ).status_code
            == 404
        )


def test_stale_verify_form_conflicts_and_stays_unsendable(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
) -> None:
    """A verify action bound to a superseded destination version conflicts.

    The operator reviewed version 1; a concurrent edit saved version 2.
    The stale form must not attest the unseen replacement, and the
    unverified destination stays unsendable at both boundaries.
    """
    del recorded_dispatch
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)
    receptionist = create_receptionist(rbac_graph)
    client = Client()
    url = f"/intake/clinics/{rbac_graph.clinic_a}/contacts/"
    with runtime_role():
        assert client.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )
        before = client.post(
            url,
            {"action": "manage", "enrollment_id": str(contacts.enrollment_id)},
        )
    assert before.status_code == 200
    assert b"4321" in before.content

    # The destination changes after the manage screen was rendered.
    _save(
        rbac_graph,
        contacts.enrollment_id,
        destination="+5511966667777",
        expected_version=1,
    )

    with runtime_role():
        stale = client.post(
            url,
            {
                "action": "verify",
                "enrollment_id": str(contacts.enrollment_id),
                "channel": SMS,
                "expected_version": "1",
            },
        )
        assert stale.status_code == 200
        assert (
            gettext(
                "This destination changed since you opened it. Review the "
                "current destination and verify it again."
            ).encode()
            in stale.content
        )
        # A verify body without the rendered version is malformed.
        assert (
            client.post(
                url,
                {
                    "action": "verify",
                    "enrollment_id": str(contacts.enrollment_id),
                    "channel": SMS,
                },
            ).status_code
            == 404
        )

    with service_runtime_role(), _as_manager(rbac_graph):
        overview = contact_overview(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=contacts.enrollment_id,
        )
    (state,) = overview.contacts
    assert state.destination_version == 2
    assert not state.verified

    # The unseen destination cannot be messaged: enqueue refuses and a
    # directly queued operation cancels at the send boundary.
    _choose(rbac_graph, contacts.enrollment_id, channel=SMS)
    with (
        service_runtime_role(),
        _as_manager(rbac_graph),
        pytest.raises(AutomatedMessageError),
    ):
        enqueue_automated_message(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=contacts.enrollment_id,
            purpose=REMINDER,
            provider=PROVIDER,
            idempotency_key=uuid4(),
        )
    with service_runtime_role(), _as_manager(rbac_graph):
        preference = PatientChannelPreference.objects.get(
            patient_id=contacts.patient_id,
            clinic_id=rbac_graph.clinic_a,
            purpose=REMINDER,
            channel=SMS,
        )
        operation_id = enqueue_operation(
            OperationRequest(
                channel=SMS,
                provider=PROVIDER,
                clinic_id=rbac_graph.clinic_a,
                subject_type=SUBJECT_TYPE_PREFERENCE,
                subject_id=preference.pk,
                idempotency_key=uuid4(),
            )
        )
    with service_runtime_role():
        assert _run_task(operation_id) == "cancelled"
    assert adapter.sent == []
    assert _operation(rbac_graph.organization_a, operation_id).last_error == (
        "subject_ineligible"
    )


@pytest.mark.parametrize("change", ["opt_out", "destination"])
def test_committed_change_at_the_send_cut_never_reaches_the_adapter(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A revocation committed after preparation cannot be overtaken.

    The deterministic cut invokes the real mutation service after the
    prepare transaction commits and before the unchanged send function
    resumes: the send boundary's subject lock and final recheck must
    cancel the operation without an external call.
    """
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)
    _verify(rbac_graph, contacts.enrollment_id)
    _choose(rbac_graph, contacts.enrollment_id, channel=SMS)
    operation_id = _enqueue(rbac_graph, contacts.enrollment_id)
    assert recorded_dispatch == [str(operation_id)]

    original = integration._send_and_finish

    def at_cut(
        scope: OperationScope,
        send_adapter: SendAdapter,
        prepared: object,
    ) -> integration.ExecutionResult:
        assert not connection.in_atomic_block
        with _as_manager(rbac_graph):
            if change == "opt_out":
                set_purpose_channel(
                    clinic_id=rbac_graph.clinic_a,
                    enrollment_id=contacts.enrollment_id,
                    purpose=REMINDER,
                    channel=None,
                )
            else:
                save_contact_destination(
                    clinic_id=rbac_graph.clinic_a,
                    enrollment_id=contacts.enrollment_id,
                    channel=SMS,
                    destination="+5511966667777",
                    expected_version=1,
                )
        with _as_manager(rbac_graph):
            assert not preference_send_eligible(scope)
        return original(scope, send_adapter, prepared)

    monkeypatch.setattr(integration, "_send_and_finish", at_cut)
    with service_runtime_role():
        assert _run_task(operation_id) == "cancelled"

    assert adapter.sent == []
    operation = _operation(rbac_graph.organization_a, operation_id)
    assert operation.status == CANCELLED
    assert operation.last_error == "subject_ineligible"


def test_new_preference_during_invalidation_never_reaches_the_adapter(
    rbac_graph: RbacGraph,
    adapter: RecordingAdapter,
    recorded_dispatch: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preference created mid-invalidation cannot overtake the send cut.

    The deterministic schedule runs the real worker on its own
    connection while the destination mutation is paused between its
    lock acquisition and commit: the worker's preference create,
    enqueue and final recheck all observe the pre-commit state, then
    the invalidation commits. The send boundary's stable
    patient-channel lock must still cancel the operation before the
    adapter call.
    """
    del recorded_dispatch
    contacts = _seed_patients(rbac_graph)
    _save(rbac_graph, contacts.enrollment_id)
    _verify(rbac_graph, contacts.enrollment_id)
    final_recheck = Event()
    mutation_committed = Event()
    original_save = PatientContact.save
    original_decision = integration._presend_decision
    futures: list[Future[str]] = []
    observation: dict[str, object] = {}

    def decision(scope: OperationScope) -> str:
        result = original_decision(scope)
        observation["presend_decision"] = result
        final_recheck.set()
        assert mutation_committed.wait(10), "mutation did not commit"
        with _as_manager(rbac_graph):
            observation["eligible_after_commit"] = preference_send_eligible(scope)
        return result

    monkeypatch.setattr(integration, "_presend_decision", decision)

    def worker() -> str:
        try:
            _choose(rbac_graph, contacts.enrollment_id)
            operation_id = _enqueue(rbac_graph, contacts.enrollment_id)
            with service_runtime_role():
                return str(integration.execute_operation(operation_id))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:

        def save_at_cut(
            self: PatientContact,
            *,
            force_insert: bool | tuple[ModelBase, ...] = False,
            force_update: bool = False,
            using: str | None = None,
            update_fields: Iterable[str] | None = None,
        ) -> None:
            if self.patient_id == contacts.patient_id and self.destination_version == 2:
                # All existing preference locks have already been
                # enumerated; there were no preferences when that query
                # executed.
                futures.append(executor.submit(worker))
                assert final_recheck.wait(10), "sender did not reach final recheck"
            original_save(
                self,
                force_insert=force_insert,
                force_update=force_update,
                using=using,
                update_fields=update_fields,
            )

        monkeypatch.setattr(PatientContact, "save", save_at_cut)
        try:
            changed = _save(
                rbac_graph,
                contacts.enrollment_id,
                destination="+5511966667777",
                expected_version=1,
            )
            observation["committed_version"] = changed.destination_version
            observation["verified"] = changed.is_verified
        finally:
            mutation_committed.set()
        observation["task_result"] = futures[0].result(timeout=10)
    observation["send_count"] = len(adapter.sent)
    assert observation["task_result"] == "cancelled"
    assert adapter.sent == [], "committed invalidation overtaken by new-preference send"
