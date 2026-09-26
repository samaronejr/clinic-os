"""Real, shared non-staff protocol subjects; the replay actor has no membership."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.billing.services import release_invoice
from apps.consent import services as consent
from apps.identity.models import User
from apps.intake.patient_access import issue_invitation, patient_session_context
from apps.prescription.signature_provider import SyntheticSignatureProvider
from apps.prescription.verification import release_document
from apps.scheduling import patient_booking, waitlist
from apps.scheduling.services import AppointmentLocalRange, create_appointment
from apps.teleconsult import services as teleconsult
from apps.teleconsult.models import TeleconsultCredential
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import encrypt
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.db import transaction
from django.test import RequestFactory

from identity.legacy_operational_boundaries import seed_operational
from identity.legacy_parity_support import world
from identity.legacy_prescription_boundaries import seed_prescription
from identity.legacy_teleconsult_boundaries import seed_teleconsult
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_document_verification import SignedSetup

if TYPE_CHECKING:
    import pytest
    from apps.consent.models import ConsentAcceptance
    from apps.consent.services import ConsentAuthority
    from apps.intake.patient_access import IssuedInvitation
    from apps.scheduling.models import Appointment, WaitlistOffer
    from apps.scheduling.patient_booking import PatientSlot
    from django.http import HttpRequest

    from identity.legacy_operational_boundaries import OperationalSubjects
    from identity.legacy_parity_support import LegacyWorld
    from identity.legacy_prescription_boundaries import PrescriptionSubjects
    from identity.legacy_teleconsult_boundaries import TeleconsultSubjects
    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class NonstaffSubjects:
    legacy: LegacyWorld
    actor: User
    op: OperationalSubjects
    rx: PrescriptionSubjects
    tc: TeleconsultSubjects
    invitation: IssuedInvitation
    acceptance: ConsentAcceptance
    authority: ConsentAuthority
    consent_offer: str
    appointment: Appointment
    slot: PatientSlot
    waitlist_offer: WaitlistOffer
    credential: TeleconsultCredential
    encrypted: bytes
    ops_value: str

    def request(self, data: dict[str, str] | None = None) -> HttpRequest:
        factory = RequestFactory()
        request = (
            factory.get("/patient/")
            if data is None
            else factory.post("/patient/", data)
        )
        request.user = self.actor
        # Use an independent session object per invocation; no login signal or
        # role-dependent middleware may reject before the named target runs.
        request.session = SessionStore()
        return request


def seed_nonstaff(
    graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> NonstaffSubjects:
    w = world(graph, "physician")
    op = seed_operational(w)
    rx = seed_prescription(w)
    tc = seed_teleconsult(w, op, monkeypatch)
    signed = SignedSetup(
        graph,
        {
            "clinic_id": w.clinic,
            "encounter_id": w.encounter,
            "patient_id": w.appointment.patient_id,
            "issuer_id": graph.physician,
        },
        rx.document,
        w.request,
        SyntheticSignatureProvider(),
    )
    signed.issue()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        release_document(clinic_id=w.clinic, document_id=rx.document.pk)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        release_invoice(clinic_id=w.clinic, invoice_id=op.invoice.pk)
        invitation = issue_invitation(clinic_id=w.clinic, enrollment_id=op.enrollment)
        appointment = create_appointment(
            clinic_id=w.clinic,
            enrollment_id=op.enrollment,
            practitioner_id=graph.physician,
            local_range=AppointmentLocalRange("2035-06-03T08:00", "2035-06-03T08:30"),
            idempotency_key=uuid4(),
        )
        waitlist.add_waitlist_entry(
            clinic_id=w.clinic,
            enrollment_id=op.enrollment,
            practitioner_id=graph.physician,
            start_local="2035-06-03T08:30",
            end_local="2035-06-03T09:00",
        )
        offered = waitlist.issue_waitlist_offer(
            clinic_id=w.clinic,
            practitioner_id=graph.physician,
            start_local="2035-06-03T08:30",
            end_local="2035-06-03T09:00",
        )
    assert offered is not None
    with runtime_role(), patient_session_context(op.patient_session):
        authority = consent.patient_authority()
        text = consent.available_texts()[0]
        _, offer = consent.prepare_acceptance(text_id=text.pk)
        acceptance = consent.record_consent(
            offer=offer, purpose=text.purpose, accepted=True
        )
        slot = next(
            s
            for s in patient_booking.patient_slots(date(2035, 6, 3), appointment.pk)
            if s.start_at.hour == 8 and s.start_at.minute == 30
        )
        joined = teleconsult.request_patient_join(session_id=tc.session.pk)
        # Retrieve the actual patient credential, not the physician credential
        # supplied by the legacy staff fixture.
        credential = TeleconsultCredential.objects.select_related("session").get(
            token_digest=hashlib.sha256(joined.token.encode()).hexdigest(),
        )
    with owner_context(graph.organization_a):
        assert w.appointment.clinic.pk == w.clinic
        encrypted = encrypt(purpose="synthetic.differential", plaintext=b"Sintetico")
    actor = User.objects.create(username=f"synthetic-differential-{uuid4().hex}")
    return NonstaffSubjects(
        w,
        actor,
        op,
        rx,
        tc,
        invitation,
        acceptance,
        authority,
        offer,
        appointment,
        slot,
        offered,
        credential,
        encrypted,
        "synthetic-ops-" + uuid4().hex,
    )


def patient_context_probe(data: NonstaffSubjects) -> bool:
    with patient_session_context(data.op.patient_session) as binding:
        # The production entry point owns a durable transaction. Roll it back
        # inside that boundary rather than replacing its transaction semantics.
        transaction.set_rollback(True)
        return binding is not None
