"""Cross-domain authorization, privacy and reliability gates.

One synthetic workflow spans intake, scheduling, EHR documents, consent,
teleconsult jobs, retention release and billing payments across two
organizations and three clinics. Staff act only through ``tenant_context``
as ``clinic_app``; patients act only through ``patient_session_context``;
jobs and callbacks run through the committed outbox boundary. Every value
is synthetic and non-identifying.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.billing.adapters import (
    AuthenticatedPaymentEvent,
    PaymentEventAuthenticationError,
    PaymentOperationFacts,
    ProviderChargeStatus,
)
from apps.billing.models import Invoice, PaymentEvent, PixCharge, Receipt, Settlement
from apps.billing.pix import complete_pix_charge, prepare_pix_charge
from apps.billing.reconciliation import (
    clear_payment_event_adapters,
    receive_payment_event,
    register_payment_event_adapter,
)
from apps.billing.services import (
    BillingAccessDeniedError,
    create_invoice,
    issue_invoice,
    patient_charges,
    release_invoice,
    view_invoice,
)
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.consent.services import publish_text
from apps.core.integration import (
    OperationRequest,
    clear_integration_registrations,
    enqueue_operation,
    register_send_adapter,
)
from apps.ehr.finalization import finalize_version
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    create_draft,
    open_encounter,
    publish_template,
    record_clinical_note,
    view_version,
)
from apps.identity.models import User, UserClinicRole
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import Patient
from apps.intake.patient_access import (
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient, issue_invitation
from apps.retention.services import patient_released_records, release_version
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    AvailabilityAccessDeniedError,
)
from apps.scheduling.models import PatientBookingEvent
from apps.scheduling.patient_booking import (
    book_patient_slot,
    patient_appointment,
    patient_slots,
)
from apps.scheduling.services import (
    AppointmentLocalRange,
    create_appointment,
    create_availability,
    view_agenda,
    view_appointment_for_transition,
)
from apps.teleconsult.adapters import SyntheticRoomAdapter
from apps.teleconsult.models import TeleconsultRoom, TeleconsultSession
from apps.teleconsult.services import (
    TeleconsultAccessDeniedError,
    create_session,
    derived_state,
    enter_room,
    request_patient_join,
    request_physician_join,
)
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django.db.utils import DatabaseError
from django.test.utils import CaptureQueriesContext
from psycopg.errors import InsufficientPrivilege

from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from renewal.test_consent import accept as accept_text
from renewal.test_encounters import setup_context
from stepup_test_support import verified_request

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pytest_django.fixtures import SettingsWrapper

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

PAYMENT_PROVIDER: Final = "synthetic-pix-v1"
PAYMENT_SECRET: Final = "synthetic-payment-secret"  # noqa: S105 - rehearsal token
CONSENT_TEXT: Final = "Texto sintético de teleconsulta.\n\nSem gravação."
SOAP_CONTENT: Final = {
    "subjective": "Relato sintético",
    "objective": "Exame sintético",
    "assessment": "Avaliação sintética",
    "plan": "Plano sintético",
}
MEASUREMENT_ENV: Final = "CLINIC_BOUNDARY_MEASUREMENTS"
# Distinctive synthetic values that must never reach application or worker
# logs: clinical content, patient details and credentials.
SENSITIVE_CANARIES: Final = (
    "Paciente Sintético A",
    "Paciente Sintético B",
    "Paciente Sintético Revogação",
    "Paciente Sintético Negado",
    "Relato sintético",
    "Exame sintético",
    "Avaliação sintética",
    "Plano sintético",
    "Texto sintético de teleconsulta",
    PAYMENT_SECRET,
    RBAC_RAW_CREDENTIAL,
)


class ScriptedPaymentAdapter:
    """HMAC-authenticating provider double with a scripted settled lookup."""

    provider: str = PAYMENT_PROVIDER

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> AuthenticatedPaymentEvent:
        """Verify the signature and return only verified event fields."""
        signature = headers.get("x-synthetic-pix-signature", "")
        expected = hmac.new(PAYMENT_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PaymentEventAuthenticationError
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise PaymentEventAuthenticationError from error
        if type(payload) is not dict:
            raise PaymentEventAuthenticationError
        event_id = payload.get("event_id")
        provider_reference = payload.get("provider_reference")
        status = payload.get("status")
        amount_minor = payload.get("amount_minor")
        currency = payload.get("currency")
        if (
            type(event_id) is not str
            or not event_id
            or type(provider_reference) is not str
            or not provider_reference
            or status not in ("pending", "settled", "expired", "cancelled", "reversed")
            or type(amount_minor) is not int
            or amount_minor <= 0
            or type(currency) is not str
            or len(currency) != 3
        ):
            raise PaymentEventAuthenticationError
        return AuthenticatedPaymentEvent(
            event_id=event_id,
            provider_reference=provider_reference,
            status=status,
            amount_minor=amount_minor,
            currency=currency,
        )

    def lookup(self, operation: PaymentOperationFacts) -> ProviderChargeStatus:
        """Answer authoritatively; the call must see no open transaction."""
        assert not connection.in_atomic_block
        return ProviderChargeStatus(
            status="settled",
            amount_minor=operation.amount_minor,
            currency=operation.currency,
        )


@dataclass(frozen=True, slots=True)
class BoundaryGraph:
    """Two organizations, three clinics and the cross-domain principals."""

    organization_a: UUID
    organization_b: UUID
    clinic_a: UUID
    clinic_b: UUID
    clinic_c: UUID
    physician_a: UUID
    receptionist_a: UUID
    admin_b: UUID
    owner_a: UUID
    manager_b: UUID
    revocable: UUID
    shared_user: UUID


@dataclass(frozen=True, slots=True)
class Workflow:
    """Artifacts of the complete synthetic cross-domain workflow."""

    graph: BoundaryGraph
    patient_a: UUID
    enrollment_a: UUID
    appointment_a: UUID
    encounter_a: UUID
    version_a: UUID
    release_a: UUID
    patient_session_a: UUID
    teleconsult_a: UUID
    room_operation_a: UUID
    invoice_a: UUID
    receipt_a: UUID
    payment_event_a: UUID
    patient_appointment_a: UUID
    patient_b: UUID
    enrollment_b: UUID
    appointment_b: UUID
    encounter_b: UUID
    invoice_b: UUID
    patient_session_b: UUID


def _user(username: str) -> User:
    return User.objects.create(
        username=f"todo42-{username}-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )


def _grant(user_id: UUID, organization_id: UUID, clinic_id: UUID, role: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        UserClinicRole.objects.create(
            user_id=user_id,
            organization_id=organization_id,
            clinic_id=clinic_id,
            role=role,
        )


@pytest.fixture
def boundary_graph(rbac_graph: RbacGraph) -> BoundaryGraph:
    """Extend the shared RBAC graph with owner/manager/revocable principals."""
    owner_a = _user("owner-a")
    manager_b = _user("manager-b")
    revocable = _user("revocable")
    _grant(owner_a.pk, rbac_graph.organization_a, rbac_graph.clinic_a, "owner")
    _grant(manager_b.pk, rbac_graph.organization_b, rbac_graph.clinic_c, "clinic_admin")
    _grant(revocable.pk, rbac_graph.organization_a, rbac_graph.clinic_a, "receptionist")
    _grant(revocable.pk, rbac_graph.organization_a, rbac_graph.clinic_b, "receptionist")
    return BoundaryGraph(
        organization_a=rbac_graph.organization_a,
        organization_b=rbac_graph.organization_b,
        clinic_a=rbac_graph.clinic_a,
        clinic_b=rbac_graph.clinic_b,
        clinic_c=rbac_graph.clinic_c,
        physician_a=rbac_graph.physician,
        receptionist_a=rbac_graph.shared_user,
        admin_b=rbac_graph.clinic_admin,
        owner_a=owner_a.pk,
        manager_b=manager_b.pk,
        revocable=revocable.pk,
        shared_user=rbac_graph.shared_user,
    )


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record outbox dispatches; jobs run explicitly through the task body."""
    sent: list[str] = []
    monkeypatch.setattr(
        execute_operation_task,
        "apply_async",
        lambda **kwargs: sent.append(kwargs["kwargs"]["operation_id"]),
    )
    return sent


@pytest.fixture
def room_adapter(settings: SettingsWrapper) -> Iterator[SyntheticRoomAdapter]:
    """Enable the synthetic room gate and re-register the boundary adapter."""
    settings.TELECONSULT_SYNTHETIC_PROVIDER = True
    adapter = SyntheticRoomAdapter()
    register_send_adapter(adapter)
    try:
        yield adapter
    finally:
        clear_integration_registrations()


@pytest.fixture
def payment_adapter(settings: SettingsWrapper) -> Iterator[ScriptedPaymentAdapter]:
    """Enable the synthetic PIX gate and register the scripted adapter."""
    settings.CLINIC_DATA_MODE = "synthetic"
    settings.BILLING_SYNTHETIC_PIX = True
    adapter = ScriptedPaymentAdapter()
    register_payment_event_adapter(adapter)
    try:
        yield adapter
    finally:
        clear_payment_event_adapters()


def _audit_rows(
    organization_id: UUID, record_id: UUID
) -> list[tuple[str, UUID | None, str]]:
    with setup_context(organization_id):
        return sorted(
            AuditEvent.objects.filter(
                organization_id=organization_id,
                affected_record_id=str(record_id),
            ).values_list("event_type", "actor_user_id", "payload__object_verb")
        )


def _run_job(operation_id: UUID) -> str:
    result = execute_operation_task.apply(kwargs={"operation_id": str(operation_id)})
    return str(result.result)


def _assert_no_sensitive_logging(caplog: pytest.LogCaptureFixture, *extra: str) -> None:
    """Assert no canary reached any captured application or worker log.

    Capture stays at INFO: ``django.db.backends`` logs SQL parameters at
    DEBUG, which is framework query logging, not application logging, and
    would make the check meaningless.
    """
    for canary in (*SENSITIVE_CANARIES, *extra):
        assert canary not in caplog.text


def _signed_payment_event(
    *,
    event_id: str,
    provider_reference: str,
    status: str = "settled",
    amount_minor: int = 12345,
    extra_claims: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], bytes]:
    payload: dict[str, object] = {
        "event_id": event_id,
        "provider_reference": provider_reference,
        "status": status,
        "amount_minor": amount_minor,
        "currency": "BRL",
    }
    if extra_claims:
        payload.update(extra_claims)
    body = json.dumps(payload).encode()
    signature = hmac.new(PAYMENT_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"x-synthetic-pix-signature": signature}, body


def _seed_org_b(graph: BoundaryGraph) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID]:
    """Seed the second tenant: patient, booking, encounter, charge, session."""
    with runtime_role(), tenant_context(graph.manager_b, graph.organization_b):
        registration = create_patient(
            clinic_id=graph.clinic_c,
            full_name="Paciente Sintético B",
            birth_date=date(1988, 7, 4),
            idempotency_key=uuid4(),
        )
        create_availability(
            clinic_id=graph.clinic_c,
            practitioner_id=graph.shared_user,
            start_local="2035-07-07T08:00",
            end_local="2035-07-07T12:00",
            idempotency_key=uuid4(),
        )
        appointment_b = create_appointment(
            clinic_id=graph.clinic_c,
            enrollment_id=registration.enrollment.pk,
            practitioner_id=graph.shared_user,
            local_range=AppointmentLocalRange("2035-07-07T09:00", "2035-07-07T09:30"),
            idempotency_key=uuid4(),
        )
        invoice_b = create_invoice(
            clinic_id=graph.clinic_c,
            patient_id=registration.patient.pk,
            amount_minor=22200,
            idempotency_key=uuid4(),
        )
        invitation_b = issue_invitation(
            clinic_id=graph.clinic_c,
            enrollment_id=registration.enrollment.pk,
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        encounter_b = open_encounter(
            clinic_id=graph.clinic_c, appointment_id=appointment_b.pk
        )
    with runtime_role():
        session_b = redeem_invitation(graph.clinic_c, invitation_b.secret)
    assert session_b is not None
    return (
        registration.patient.pk,
        registration.enrollment.pk,
        appointment_b.pk,
        encounter_b.pk,
        invoice_b.pk,
        session_b,
    )


def _staff_chart(graph: BoundaryGraph, appointment_id: UUID) -> tuple[UUID, UUID, UUID]:
    """Open the encounter, write the note, finalize and release the record."""
    with runtime_role(), tenant_context(graph.owner_a, graph.organization_a):
        template = publish_template(
            clinic_id=graph.clinic_a,
            key="geral",
            title="Clínica geral",
            prompts=dict.fromkeys(SOAP_CONTENT, "Registro do médico"),
        )
    request = verified_request(graph.physician_a)
    with runtime_role(), tenant_context(graph.physician_a, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment_id
        )
        version = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
        version = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=SOAP_CONTENT,
        )
        finalized = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        release = release_version(clinic_id=graph.clinic_a, version_id=finalized.pk)
    return encounter.pk, finalized.pk, release.pk


def _teleconsult_round_trip(
    graph: BoundaryGraph, encounter_id: UUID, patient_session: UUID
) -> tuple[UUID, UUID]:
    """Create the session, run the room job and admit both participants."""
    with runtime_role(), tenant_context(graph.physician_a, graph.organization_a):
        session = create_session(clinic_id=graph.clinic_a, encounter_id=encounter_id)
    with setup_context(graph.organization_a):
        operation_id = TeleconsultRoom.objects.get(session_id=session.pk).operation_id
    # Service principal: the committed outbox job creates the room.
    with runtime_role():
        assert _run_job(operation_id) == "succeeded"
    with runtime_role(), tenant_context(graph.physician_a, graph.organization_a):
        physician_token = request_physician_join(
            clinic_id=graph.clinic_a, session_id=session.pk
        ).token
        physician_entry = enter_room(token=physician_token, role="physician")
    with runtime_role(), patient_session_context(patient_session):
        patient_token = request_patient_join(session_id=session.pk).token
        patient_entry = enter_room(token=patient_token, role="patient")
    assert patient_entry.room_name == physician_entry.room_name
    return session.pk, operation_id


def _settled_charge(graph: BoundaryGraph, patient_id: UUID) -> tuple[UUID, UUID, UUID]:
    """Charge, rehearse PIX, release and settle through the provider event."""
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        invoice = create_invoice(
            clinic_id=graph.clinic_a,
            patient_id=patient_id,
            amount_minor=12345,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=graph.clinic_a,
            invoice_id=invoice.pk,
            expected_revision=1,
        )
        pix_operation = prepare_pix_charge(
            clinic_id=graph.clinic_a, invoice_id=invoice.pk
        )
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        charge = complete_pix_charge(
            clinic_id=graph.clinic_a,
            invoice_id=invoice.pk,
            operation_id=pix_operation.pk,
        )
        release_invoice(clinic_id=graph.clinic_a, invoice_id=invoice.pk)
    # Service principal: the authenticated provider event settles once; its
    # foreign-tenant claim is ignored.
    headers, body = _signed_payment_event(
        event_id="evt-e2e-1",
        provider_reference=charge.provider_reference,
        extra_claims={"organization_id": str(graph.organization_b)},
    )
    with runtime_role():
        assert (
            receive_payment_event(provider=PAYMENT_PROVIDER, headers=headers, body=body)
            == "settled"
        )
    # Billing tables are function-guarded; read them as the runtime role.
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        receipt = Receipt.objects.get(settlement__invoice_id=invoice.pk)
        payment_event_id = PaymentEvent.objects.get(
            provider_reference=charge.provider_reference
        ).pk
    return invoice.pk, receipt.pk, payment_event_id


@pytest.fixture
def workflow(
    boundary_graph: BoundaryGraph,
    dispatched: list[str],
    room_adapter: SyntheticRoomAdapter,
    payment_adapter: ScriptedPaymentAdapter,
) -> Workflow:
    """Execute the complete synthetic workflow across both tenants."""
    graph = boundary_graph
    # Staff principal: intake, scheduling and clinical record in clinic A.
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        registration = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Paciente Sintético A",
            birth_date=date(1991, 2, 3),
            idempotency_key=uuid4(),
        )
        create_availability(
            clinic_id=graph.clinic_a,
            practitioner_id=graph.physician_a,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
        appointment = create_appointment(
            clinic_id=graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            practitioner_id=graph.physician_a,
            local_range=AppointmentLocalRange("2035-06-02T09:00", "2035-06-02T09:30"),
            idempotency_key=uuid4(),
        )
    with runtime_role(), tenant_context(graph.owner_a, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="teleconsultation",
            text=CONSENT_TEXT,
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
    encounter_id, version_id, release_id = _staff_chart(graph, appointment.pk)
    # Patient principal: redeem the invitation, consent, book a slot.
    with runtime_role():
        patient_session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert patient_session is not None
    with runtime_role(), patient_session_context(patient_session) as binding:
        assert binding is not None
        accept_text(text)
        slots = patient_slots(date(2035, 6, 2))
        assert slots
        booked = book_patient_slot(token=slots[0].token, idempotency_key=uuid4())
    session_id, operation_id = _teleconsult_round_trip(
        graph, encounter_id, patient_session
    )
    invoice_id, receipt_id, payment_event_id = _settled_charge(
        graph, registration.patient.pk
    )
    (
        patient_b,
        enrollment_b,
        appointment_b,
        encounter_b,
        invoice_b,
        session_b,
    ) = _seed_org_b(graph)
    return Workflow(
        graph=graph,
        patient_a=registration.patient.pk,
        enrollment_a=registration.enrollment.pk,
        appointment_a=appointment.pk,
        encounter_a=encounter_id,
        version_a=version_id,
        release_a=release_id,
        patient_session_a=patient_session,
        teleconsult_a=session_id,
        room_operation_a=operation_id,
        invoice_a=invoice_id,
        receipt_a=receipt_id,
        payment_event_a=payment_event_id,
        patient_appointment_a=booked.pk,
        patient_b=patient_b,
        enrollment_b=enrollment_b,
        appointment_b=appointment_b,
        encounter_b=encounter_b,
        invoice_b=invoice_b,
        patient_session_b=session_b,
    )


def test_complete_workflow_audit_lineage_and_bounded_jobs(
    workflow: Workflow,
    dispatched: list[str],
) -> None:
    """Every domain append lands on the right tenant chain with the right actor."""
    graph = workflow.graph
    # Staff actions carry the staff actor on the organization-A chain.
    staff_expectations = {
        workflow.patient_a: ("intake.patient.created", graph.receptionist_a),
        workflow.appointment_a: (
            "scheduling.appointment.created",
            graph.receptionist_a,
        ),
        workflow.encounter_a: ("ehr.encounter.opened", graph.physician_a),
        workflow.version_a: ("ehr.document.finalized", graph.physician_a),
        workflow.release_a: ("ehr.document.released", graph.physician_a),
        workflow.teleconsult_a: ("teleconsult.session.created", graph.physician_a),
        workflow.room_operation_a: ("comms.operation.succeeded", graph.physician_a),
        workflow.payment_event_a: ("billing.payment.settled", graph.receptionist_a),
    }
    for record_id, (event_type, actor) in staff_expectations.items():
        rows = _audit_rows(graph.organization_a, record_id)
        assert (event_type, actor, event_type.rsplit(".", 1)[1]) in rows
    # The room job dispatched exactly once through the committed outbox.
    assert dispatched == [str(workflow.room_operation_a)]
    # The patient booking receipt is database-owned and session-bound.
    with setup_context(graph.organization_a):
        booking_events = list(
            PatientBookingEvent.objects.filter(
                appointment_id=workflow.patient_appointment_a
            )
        )
        assert len(booking_events) == 1
        assert booking_events[0].patient_session_id == workflow.patient_session_a
        assert booking_events[0].action == "created"
    # The foreign-tenant claim on the payment event changed nothing in B.
    assert _audit_rows(graph.organization_b, workflow.payment_event_a) == []
    # Both tenant chains verify end to end.
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        chain_a = verify_chain(graph.organization_a)
    with runtime_role(), tenant_context(graph.manager_b, graph.organization_b):
        chain_b = verify_chain(graph.organization_b)
    assert chain_a.valid
    assert chain_a.row_count > 0
    assert chain_b.valid
    assert chain_b.row_count > 0


def test_every_tenant_table_hides_the_other_organization(
    workflow: Workflow,
    app_database_url: str,
) -> None:
    """FORCE-RLS tenant tables expose zero rows of the other organization.

    The probe enumerates every FORCE-RLS table carrying an ``organization_id``
    column, so a newly added tenant table is collected automatically; tables
    the runtime role cannot read at all count as denied.
    """
    graph = workflow.graph
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT class.relname, attribute.attname
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            JOIN pg_catalog.pg_attribute AS attribute
              ON attribute.attrelid = class.oid
             AND attribute.attnum > 0
             AND attribute.attname = CASE
                   WHEN class.relname = 'identity_organization'
                   THEN 'id' ELSE 'organization_id' END
            WHERE namespace.nspname = 'clinic_app'
              AND class.relkind = 'r'
              AND class.relrowsecurity
              AND class.relforcerowsecurity
            ORDER BY class.relname
            """
        )
        table_columns = [(row[0], row[1]) for row in cursor.fetchall()]
    tables = [table for table, _column in table_columns]

    # The workflow seeded rows in every exercised domain's tenant tables.
    seeded_tables = {
        "billing_invoice",
        "billing_paymentevent",
        "billing_pixcharge",
        "billing_pixoperation",
        "billing_receipt",
        "billing_settlement",
        "comms_integrationoperation",
        "consent_consentacceptance",
        "consent_consenttext",
        "ehr_clinicaldocument",
        "ehr_clinicaldocumentversion",
        "ehr_encounter",
        "ehr_specialtytemplate",
        "identity_clinic",
        "identity_organization",
        "identity_userclinicrole",
        "intake_patient",
        "intake_patientaccessgrant",
        "intake_patientclinicenrollment",
        "intake_patientsession",
        "retention_recordrelease",
        "scheduling_appointment",
        "scheduling_availabilityblock",
        "scheduling_patientbookingevent",
        "teleconsult_teleconsultcredential",
        "teleconsult_teleconsultroom",
        "teleconsult_teleconsultsession",
    }
    assert seeded_tables <= set(tables)

    with psycopg.connect(app_database_url) as app_connection:
        for table, tenant_column in table_columns:
            for organization_id in (graph.organization_a, graph.organization_b):
                other = (
                    graph.organization_b
                    if organization_id == graph.organization_a
                    else graph.organization_a
                )
                app_connection.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    (str(organization_id),),
                )
                try:
                    row = app_connection.execute(
                        f'SELECT count(*) FROM clinic_app."{table}" '  # noqa: S608
                        f'WHERE "{tenant_column}" = %s',
                        (other,),
                    ).fetchone()
                    visible = row[0] if row else 0
                except InsufficientPrivilege:
                    visible = 0
                assert visible == 0, f"{table} leaked into {organization_id}"
                app_connection.rollback()
        # With no tenant GUC every tenant table fails closed.
        for table in tables:
            try:
                row = app_connection.execute(
                    f'SELECT count(*) FROM clinic_app."{table}"'  # noqa: S608
                ).fetchone()
                visible = row[0] if row else 0
            except InsufficientPrivilege:
                visible = 0
            assert visible == 0, f"{table} visible without a tenant"
            app_connection.rollback()


def test_swapped_references_are_denied_across_domains(
    workflow: Workflow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Foreign organization/clinic/patient/document/payment ids never resolve."""
    caplog.set_level(logging.INFO)
    graph = workflow.graph
    # A tenant context for the other organization sees none of A's records.
    with runtime_role(), tenant_context(graph.manager_b, graph.organization_b):
        with pytest.raises(BillingAccessDeniedError):
            view_invoice(clinic_id=graph.clinic_c, invoice_id=workflow.invoice_a)
        with pytest.raises(ClinicalAccessDeniedError):
            view_version(clinic_id=graph.clinic_c, version_id=workflow.version_a)
        with pytest.raises(ClinicalAccessDeniedError):
            open_encounter(
                clinic_id=graph.clinic_c, appointment_id=workflow.appointment_a
            )
        with pytest.raises(TeleconsultAccessDeniedError):
            request_physician_join(
                clinic_id=graph.clinic_c, session_id=workflow.teleconsult_a
            )
        assert not Patient.objects.filter(pk=workflow.patient_a).exists()
    # Same organization, wrong clinic: clinic B staff cannot reach clinic A.
    with runtime_role(), tenant_context(graph.admin_b, graph.organization_a):
        with pytest.raises(BillingAccessDeniedError):
            view_invoice(clinic_id=graph.clinic_b, invoice_id=workflow.invoice_a)
        with pytest.raises(ClinicalAccessDeniedError):
            view_version(clinic_id=graph.clinic_b, version_id=workflow.version_a)
    # A foreign patient id cannot anchor a charge in clinic A.
    with (
        runtime_role(),
        tenant_context(graph.receptionist_a, graph.organization_a),
        pytest.raises(DatabaseError),
    ):
        create_invoice(
            clinic_id=graph.clinic_a,
            patient_id=workflow.patient_b,
            amount_minor=100,
            idempotency_key=uuid4(),
        )
    # A foreign appointment id cannot anchor an encounter in clinic A.
    with (
        runtime_role(),
        tenant_context(graph.physician_a, graph.organization_a),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        open_encounter(clinic_id=graph.clinic_a, appointment_id=workflow.appointment_b)
    # A user with no membership in organization B cannot open its context.
    with (
        runtime_role(),
        pytest.raises(TenantAccessDeniedError),
        tenant_context(graph.physician_a, graph.organization_b),
    ):
        pass
    # An invitation code redeems only in the clinic that issued it.
    with runtime_role():
        assert redeem_invitation(graph.clinic_c, "wrong-code") is None
    # Denials never log clinical content, patient details or credentials.
    _assert_no_sensitive_logging(caplog)


def test_patient_principal_cannot_cross_enrollment_or_tenant(
    workflow: Workflow,
) -> None:
    """The patient session resolves only its own bound enrollment."""
    with (
        runtime_role(),
        patient_session_context(workflow.patient_session_a) as binding,
    ):
        assert binding is not None
        # Another patient's appointment id is non-enumerating.
        with pytest.raises(AppointmentAccessDeniedError):
            patient_appointment(workflow.appointment_b)
        # Released records resolve only for the bound enrollment.
        records = patient_released_records()
        assert [record.version_id for record in records] == [workflow.version_a]
        # Charges resolve only for the bound patient and released invoices.
        charges = patient_charges()
        assert [charge.invoice_id for charge in charges] == [workflow.invoice_a]
        assert charges[0].receipt_reference is not None
    with (
        runtime_role(),
        patient_session_context(workflow.patient_session_b) as binding_b,
    ):
        assert binding_b is not None
        assert patient_released_records() == []
        assert patient_charges() == ()
        with pytest.raises(AppointmentAccessDeniedError):
            patient_appointment(workflow.appointment_a)


def _revoke(user_id: UUID, organization_id: UUID, clinic_id: UUID) -> None:
    """Delete one clinic role through the owner role between operations."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        UserClinicRole.objects.filter(user_id=user_id, clinic_id=clinic_id).delete()


def test_revoked_role_denies_mid_operation_across_domains(
    workflow: Workflow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Losing clinic authority mid-operation closes every domain in it."""
    caplog.set_level(logging.INFO)
    graph = workflow.graph
    # Authority in clinic A works before revocation.
    with runtime_role(), tenant_context(graph.revocable, graph.organization_a):
        created = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Paciente Sintético Revogação",
            birth_date=date(1992, 4, 5),
            idempotency_key=uuid4(),
        )
        assert created.patient.pk is not None
    # The owner revokes the clinic-A role between operations.
    _revoke(graph.revocable, graph.organization_a, graph.clinic_a)
    # The same principal is now denied across domains in clinic A only.
    with runtime_role(), tenant_context(graph.revocable, graph.organization_a):
        with pytest.raises(PatientAccessDeniedError):
            create_patient(
                clinic_id=graph.clinic_a,
                full_name="Paciente Sintético Negado",
                birth_date=date(1992, 4, 6),
                idempotency_key=uuid4(),
            )
        with pytest.raises(BillingAccessDeniedError):
            create_invoice(
                clinic_id=graph.clinic_a,
                patient_id=workflow.patient_a,
                amount_minor=100,
                idempotency_key=uuid4(),
            )
        with pytest.raises(AvailabilityAccessDeniedError):
            view_agenda(clinic_id=graph.clinic_a, view="day", date="2035-06-02", page=1)
        # Clinic B authority is untouched: revocation is per clinic.
        view_agenda(clinic_id=graph.clinic_b, view="day", date="2035-06-02", page=1)
    # A queued job whose actor loses clinic authority cancels without sending.
    with runtime_role(), tenant_context(graph.revocable, graph.organization_a):
        operation_id = enqueue_operation(
            OperationRequest(
                channel=IntegrationOperation.Channel.EMAIL,
                provider="synthetic-provider",
                clinic_id=graph.clinic_b,
                subject_type="intake.patient_clinic_enrollment",
                subject_id=uuid4(),
                idempotency_key=uuid4(),
                max_attempts=2,
            )
        )
    _revoke(graph.revocable, graph.organization_a, graph.clinic_b)
    with runtime_role():
        assert _run_job(operation_id) == "cancelled"
    with setup_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(pk=operation_id)
        assert operation.status == IntegrationOperation.Status.CANCELLED
        assert operation.last_error == "authority_revoked"
    # With no membership left the tenant context itself fails closed.
    with (
        runtime_role(),
        pytest.raises(TenantAccessDeniedError),
        tenant_context(graph.revocable, graph.organization_a),
    ):
        pass
    # Revocation denials never log clinical content, patient details or
    # credentials.
    _assert_no_sensitive_logging(caplog)


def test_payment_and_job_failures_stay_bounded_and_scoped(
    workflow: Workflow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Forged, foreign and replayed provider traffic cannot move money."""
    caplog.set_level(logging.INFO)
    graph = workflow.graph
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        charge_reference = PixCharge.objects.get(
            invoice_id=workflow.invoice_a
        ).provider_reference
    # A forged signature is rejected before any charge or tenant resolves.
    rejected_headers, rejected_body = _signed_payment_event(
        event_id="evt-forged",
        provider_reference=charge_reference,
    )
    forged_headers = {"x-synthetic-pix-signature": "forged"}
    with runtime_role(), pytest.raises(PaymentEventAuthenticationError):
        receive_payment_event(
            provider=PAYMENT_PROVIDER,
            headers=forged_headers,
            body=rejected_body,
        )
    # The rejection must not log the denied body or the submitted signature.
    _assert_no_sensitive_logging(
        caplog,
        rejected_body.decode(),
        forged_headers["x-synthetic-pix-signature"],
    )
    with runtime_role(), pytest.raises(PaymentEventAuthenticationError):
        receive_payment_event(
            provider="unregistered-provider",
            headers=rejected_headers,
            body=rejected_body,
        )
    # The unregistered-provider rejection must not log the denied body or
    # its (validly signed) signature either.
    _assert_no_sensitive_logging(
        caplog,
        rejected_body.decode(),
        rejected_headers["x-synthetic-pix-signature"],
    )
    # Replaying the settled event is a duplicate, never a second settlement.
    headers, body = _signed_payment_event(
        event_id="evt-e2e-1",
        provider_reference=charge_reference,
    )
    with runtime_role():
        assert (
            receive_payment_event(provider=PAYMENT_PROVIDER, headers=headers, body=body)
            == "duplicate"
        )
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        assert Settlement.objects.filter(invoice_id=workflow.invoice_a).count() == 1
        assert (
            Receipt.objects.filter(settlement__invoice_id=workflow.invoice_a).count()
            == 1
        )
        invoice = Invoice.objects.get(pk=workflow.invoice_a)
        assert invoice.state == Invoice.State.PAID
    # The draft charge in organization B stays untouched by A's events.
    with runtime_role(), tenant_context(graph.manager_b, graph.organization_b):
        invoice_b = Invoice.objects.get(pk=workflow.invoice_b)
        assert invoice_b.state == Invoice.State.DRAFT
    # Provider failures never log the secret, signature or callback payload,
    # including the rejected callback's body and both submitted signatures.
    _assert_no_sensitive_logging(
        caplog,
        body.decode(),
        headers["x-synthetic-pix-signature"],
        rejected_body.decode(),
        forged_headers["x-synthetic-pix-signature"],
        rejected_headers["x-synthetic-pix-signature"],
    )


def test_bounded_job_retries_fail_closed(
    workflow: Workflow,
    settings: SettingsWrapper,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing room job exhausts attempts and marks the session failed."""
    caplog.set_level(logging.INFO)
    graph = workflow.graph
    settings.TELECONSULT_SYNTHETIC_FAIL = True
    with runtime_role(), tenant_context(graph.physician_a, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=workflow.patient_appointment_a
        )
        session = create_session(clinic_id=graph.clinic_a, encounter_id=encounter.pk)
    with setup_context(graph.organization_a):
        operation_id = TeleconsultRoom.objects.get(session_id=session.pk).operation_id
    with runtime_role():
        assert _run_job(operation_id) == "retry"
        assert _run_job(operation_id) == "retry"
        assert _run_job(operation_id) == "retry"
        assert _run_job(operation_id) == "failed"
    with setup_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(pk=operation_id)
        assert operation.status == IntegrationOperation.Status.FAILED
        assert operation.attempt_count == 3
        assert operation.last_error == "attempts_exhausted"
        stored = TeleconsultSession.objects.get(pk=session.pk)
        assert derived_state(stored) == "failed"
    # Bounded retries never log clinical content, patient details or
    # credentials.
    _assert_no_sensitive_logging(caplog)


BROKER_OUTAGE: Final = "synthetic broker outage"


def _open_room_session(graph: BoundaryGraph, appointment_id: UUID) -> None:
    """Open an encounter and enqueue its room session in one transaction."""
    with runtime_role(), tenant_context(graph.physician_a, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment_id
        )
        create_session(clinic_id=graph.clinic_a, encounter_id=encounter.pk)


def test_queue_dispatch_failure_leaves_committed_recoverable_operation(
    workflow: Workflow,
    dispatched: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broker outage at dispatch leaves a committed, claimable operation.

    ``enqueue_operation`` dispatches through ``transaction.on_commit``; when
    the transport raises, the exception propagates after the commit, so the
    session, room and operation rows persist and the stored operation stays
    claimable for a later redrive.
    """
    caplog.set_level(logging.INFO)
    graph = workflow.graph
    dispatched.clear()

    def _broker_down(**kwargs: object) -> None:
        raise ConnectionError(BROKER_OUTAGE)

    monkeypatch.setattr(execute_operation_task, "apply_async", _broker_down)
    with pytest.raises(ConnectionError, match=BROKER_OUTAGE):
        _open_room_session(graph, workflow.patient_appointment_a)

    # The commit already landed: the session persisted and its operation is
    # pending, never attempted, with no provider effect.
    with setup_context(graph.organization_a):
        session = TeleconsultSession.objects.get(
            appointment_id=workflow.patient_appointment_a
        )
        room = TeleconsultRoom.objects.get(session_id=session.pk)
        operation = IntegrationOperation.objects.get(pk=room.operation_id)
        assert operation.status == IntegrationOperation.Status.PENDING
        assert operation.attempt_count == 0
        assert operation.provider_reference is None
        assert session.state == TeleconsultSession.State.WAITING
    assert dispatched == []
    # The failed dispatch produced no external effect and no cross-tenant row.
    assert _audit_rows(graph.organization_b, operation.pk) == []
    with runtime_role(), tenant_context(graph.manager_b, graph.organization_b):
        assert not IntegrationOperation.objects.filter(pk=operation.pk).exists()

    # Redrive the committed row through the real dispatch boundary once the
    # transport recovers; the worker then completes it exactly once.
    monkeypatch.setattr(
        execute_operation_task,
        "apply_async",
        lambda **kwargs: dispatched.append(kwargs["kwargs"]["operation_id"]),
    )
    execute_operation_task.apply_async(kwargs={"operation_id": str(operation.pk)})
    assert dispatched == [str(operation.pk)]
    with runtime_role():
        assert _run_job(operation.pk) == "succeeded"
    with setup_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(pk=operation.pk)
        assert operation.status == IntegrationOperation.Status.SUCCEEDED
        assert operation.attempt_count == 1
        assert operation.provider_reference == f"synthetic:room:tc-{session.pk}"
    # The recovered session still admits its physician; the issued credential
    # is itself a canary that must never reach the logs.
    with runtime_role(), tenant_context(graph.physician_a, graph.organization_a):
        join_token = request_physician_join(
            clinic_id=graph.clinic_a, session_id=session.pk
        ).token
    _assert_no_sensitive_logging(caplog, join_token)


def test_agenda_and_record_queries_measured_on_documented_fixture(
    workflow: Workflow,
) -> None:
    """Measure agenda/record latency and query counts on the synthetic fixture.

    Fixture: two organizations; clinic A holds one staff booking, one patient
    booking, one finalized released document and one settled invoice; clinic C
    holds one booking, encounter and draft invoice. Counts stay inside the
    existing bounded-query contract; latency is recorded, never asserted, so
    no production SLA is invented.
    """
    graph = workflow.graph
    measurements: dict[str, dict[str, float | int]] = {}
    with runtime_role(), tenant_context(graph.receptionist_a, graph.organization_a):
        for name, call in (
            (
                "agenda_day",
                lambda: view_agenda(
                    clinic_id=graph.clinic_a,
                    view="day",
                    date="2035-06-02",
                    page=1,
                ),
            ),
            (
                "appointment_transition",
                lambda: view_appointment_for_transition(
                    appointment_id=workflow.appointment_a
                ),
            ),
        ):
            with CaptureQueriesContext(connection) as captured:
                started = time.perf_counter()
                call()
                elapsed = time.perf_counter() - started
            measurements[name] = {
                "queries": len(captured.captured_queries),
                "milliseconds": round(elapsed * 1000, 3),
            }
    with (
        runtime_role(),
        tenant_context(graph.physician_a, graph.organization_a),
        CaptureQueriesContext(connection) as record_queries,
    ):
        started = time.perf_counter()
        view_version(clinic_id=graph.clinic_a, version_id=workflow.version_a)
        elapsed = time.perf_counter() - started
    measurements["record_version"] = {
        "queries": len(record_queries.captured_queries),
        "milliseconds": round(elapsed * 1000, 3),
    }

    # Existing bounded-query contract; latency is evidence, not an SLA.
    # Envelope columns decrypt one protected_decrypt call per row: the
    # agenda page holds two appointments (two patient columns each) and
    # the record version carries its content envelope.
    assert measurements["agenda_day"]["queries"] <= 18
    assert measurements["appointment_transition"]["queries"] <= 15
    assert measurements["record_version"]["queries"] <= 16

    destination = os.environ.get(MEASUREMENT_ENV, "")
    if destination:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(measurements, indent=2, sort_keys=True) + "\n")
