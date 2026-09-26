"""Seed the smallest exact synthetic source through Phase 1A domain code.

Imports inside ``seed`` stay deferred so ``django.setup()`` runs before any
app module is touched; the fixture is a subprocess entrypoint, not a library.
"""

# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol
from uuid import UUID, uuid4

import django
from django.db import connection, transaction

ORGANIZATION_ID = UUID("10000000-0000-4000-8000-000000000020")
CLINIC_ID = UUID("20000000-0000-4000-8000-000000000020")
OWNER_ID = UUID("30000000-0000-4000-8000-000000000020")
PHYSICIAN_ID = UUID("40000000-0000-4000-8000-000000000020")
MIN_PRIVATE_FD: Final = 3
MAX_PASSWORD_BYTES: Final = 1024
PROBE_PLAINTEXT: Final = b"synthetic restore rehearsal probe payload"
PROBE_PURPOSE: Final = "restore-probe"
PDF_BYTES: Final = b"%PDF-1.4\n%synthetic clinical attachment\n%%EOF\n"
# Non-default on purpose: a restore that dropped the row reads the defaults.
RESTORED_PREFERENCES: Final = ("dark", "compact")

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.http import HttpRequest


class _BootstrapFactory(Protocol):
    def __call__(
        self,
        *args: object,
        **kwargs: object,
    ) -> object: ...


class _BootstrapAction(Protocol):
    def __call__(self, request: object, password: str) -> None: ...


class _Device(Protocol):
    persistent_id: str


class _DeviceManager(Protocol):
    def create(self, **values: object) -> _Device: ...


class _DeviceModel(Protocol):
    objects: _DeviceManager


@contextmanager
def _runtime_role() -> Iterator[None]:
    """Run one block as the application role exactly like request traffic."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()
        already_runtime = row == ("clinic_app",)
        if not already_runtime:
            cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        if not already_runtime:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")


@contextmanager
def _owner_scope(organization_id: UUID) -> Iterator[None]:
    """Set the tenant GUC inside one owner transaction for direct writes."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        yield


@contextmanager
def _owner_tenant_scope(organization_id: UUID, user_id: UUID) -> Iterator[None]:
    """Bind tenant and actor GUCs as owner for owner-only key operations.

    ``tenant_context`` resolves membership through ``user_has_org``, which is
    granted only to ``clinic_app``; owner-side key administration instead sets
    the same transaction-local GUCs the envelope functions re-validate.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(user_id)],
        )
        yield


def bootstrap(password: str) -> None:
    """Create one synthetic organization, clinic, owner role, and system audit."""
    module = import_module("apps.identity.management.bootstrap")
    factory: _BootstrapFactory = module.BootstrapRequest
    action: _BootstrapAction = module.bootstrap_clinic
    request = factory(
        organization_id=ORGANIZATION_ID,
        organization_name="Synthetic Phase 1A Recovery Clinic",
        cnpj="00000000000000",
        clinic_id=CLINIC_ID,
        clinic_name="Synthetic Recovery Clinic",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
        owner_user_id=OWNER_ID,
        owner_username="synthetic.recovery.owner",
        owner_email="recovery-owner@example.invalid",
    )
    action(request, password)


def create_totp() -> None:
    """Create one confirmed source-only TOTP row through its app-role RLS policy."""
    module = import_module("django_otp.plugins.otp_totp.models")
    device: _DeviceModel = module.TOTPDevice
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL app.current_user_id = %s", [str(OWNER_ID)])
        device.objects.create(
            user_id=OWNER_ID,
            name="synthetic-recovery-device",
            confirmed=True,
        )


def _totp_device(user_id: UUID) -> _Device:
    """Create one confirmed TOTP device for step-up-bound operations."""
    module = import_module("django_otp.plugins.otp_totp.models")
    device: _DeviceModel = module.TOTPDevice
    with _runtime_role(), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [str(user_id)],
            )
        return device.objects.create(
            user_id=user_id,
            name="synthetic-recovery-physician",
            confirmed=True,
        )


def _verified_request(user_id: UUID) -> HttpRequest:
    """Build the exact post-step-up request shape OTPMiddleware produces."""
    from apps.identity.models import User
    from apps.identity.stepup import STEP_UP_SESSION_KEY
    from django.contrib.sessions.backends.db import SessionStore
    from django.http import HttpRequest, HttpResponse
    from django_otp import DEVICE_ID_SESSION_KEY
    from django_otp.middleware import OTPMiddleware

    user = User.objects.get(pk=user_id)
    device = _totp_device(user_id)
    request = HttpRequest()
    request.session = SessionStore()
    request.session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    request.session[STEP_UP_SESSION_KEY] = int(time.time())
    request.user = user
    OTPMiddleware(lambda _request: HttpResponse())(request)
    return request


def seed(probe_path: Path) -> None:  # noqa: PLR0915 - one ordered rehearsal path
    """Exercise every domain write path once, then publish the key probe.

    The fixture runs as ``clinic_owner``; every service call goes through the
    real ``clinic_app`` runtime role and tenant/patient session contexts so
    RLS, resolver functions and audit append paths all execute exactly as in
    request traffic. The probe binds one envelope to its plaintext digest so
    the rehearsal can prove restored key material on the target.
    """
    from apps.billing.adapters import (
        SYNTHETIC_PROVIDER as PIX_PROVIDER,
    )
    from apps.billing.adapters import (
        SYNTHETIC_SIGNATURE_HEADER,
        SyntheticPixAdapter,
    )
    from apps.billing.pix import complete_pix_charge, prepare_pix_charge
    from apps.billing.reconciliation import (
        receive_payment_event,
        register_payment_event_adapter,
    )
    from apps.billing.services import create_invoice, issue_invoice, release_invoice
    from apps.consent.services import (
        prepare_acceptance,
        publish_text,
        record_consent,
    )
    from apps.core.secrets import secret_store
    from apps.ehr.attachments import (
        AttachmentInput,
        scan_attachment,
        upload_attachment,
    )
    from apps.ehr.finalization import (
        amend_document,
        close_encounter,
        discard_draft,
        finalize_version,
    )
    from apps.ehr.history import HistoryChange, save_history
    from apps.ehr.services import (
        create_draft,
        open_encounter,
        record_clinical_note,
    )
    from apps.ehr.services import (
        publish_template as publish_specialty_template,
    )
    from apps.identity.clinic_configuration import (
        ConfigurationContent,
        publish_configuration,
    )
    from apps.identity.models import PhysicianProfile, User, UserClinicRole
    from apps.identity.preferences import save_ui_preferences
    from apps.intake.contacts import (
        save_contact_destination,
        set_purpose_channel,
        verify_contact,
    )
    from apps.intake.patient_access import (
        issue_invitation,
        patient_session_context,
        redeem_invitation,
    )
    from apps.intake.patient_creation import create_patient
    from apps.intake.questionnaires import (
        assign_questionnaire,
        publish_template,
        submit_intake,
    )
    from apps.prescription.models import PrescriptionDocumentRevocation
    from apps.prescription.policy import SYNTHETIC_CATEGORY
    from apps.prescription.services import create_draft as create_rx_draft
    from apps.prescription.services import (
        discard_draft as discard_rx_draft,
    )
    from apps.prescription.services import (
        render_document,
        save_draft,
    )
    from apps.prescription.signature_provider import (
        SYNTHETIC_PROVIDER,
        SignatureRequest,
        SyntheticSignatureProvider,
    )
    from apps.prescription.signing import (
        receive_signature_callback,
        request_signature,
    )
    from apps.prescription.verification import (
        release_document,
        revoke_document,
        verify_handle,
    )
    from apps.retention.services import (
        approve_policy,
        export_staff_records,
        place_hold,
        propose_policy,
        record_disposition,
        release_version,
    )
    from apps.scheduling.availability_creation import create_availability
    from apps.scheduling.patient_booking import book_patient_slot, patient_slots
    from apps.scheduling.waitlist import add_waitlist_entry, issue_waitlist_offer
    from apps.teleconsult.services import (
        create_session,
        end_consultation,
        enter_room,
        request_patient_join,
        request_physician_join,
        start_consultation,
    )
    from apps.tenancy.db import tenant_context
    from apps.tenancy.envelope import encrypt, issue_tenant_key
    from django.contrib.auth.hashers import make_password

    User.objects.create(
        id=PHYSICIAN_ID,
        username="synthetic.recovery.physician",
        password=make_password("synthetic-not-a-credential"),
    )
    # The tenant DEK must exist before the first protected-field write.
    with _owner_tenant_scope(ORGANIZATION_ID, OWNER_ID):
        issue_tenant_key()
    with _owner_scope(ORGANIZATION_ID):
        UserClinicRole.objects.create(
            organization_id=ORGANIZATION_ID,
            clinic_id=CLINIC_ID,
            user_id=PHYSICIAN_ID,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        PhysicianProfile.objects.create(
            organization_id=ORGANIZATION_ID,
            user_id=PHYSICIAN_ID,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-43",
            signing_subject=f"synthetic:physician:{PHYSICIAN_ID}",
            synthetic=True,
        )

    with _runtime_role(), tenant_context(OWNER_ID, ORGANIZATION_ID):
        registration = create_patient(
            clinic_id=CLINIC_ID,
            full_name="Synthetic Recovery Persona",
            birth_date=date(2000, 1, 2),
            idempotency_key=uuid4(),
        )
        enrollment_id = registration.enrollment.pk
        patient_id = registration.patient.pk
        create_availability(
            clinic_id=CLINIC_ID,
            practitioner_id=PHYSICIAN_ID,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
        save_contact_destination(
            clinic_id=CLINIC_ID,
            enrollment_id=enrollment_id,
            channel="email",
            destination="synthetic-patient@example.invalid",
            expected_version=None,
        )
        verify_contact(
            clinic_id=CLINIC_ID,
            enrollment_id=enrollment_id,
            channel="email",
            expected_version=1,
        )
        set_purpose_channel(
            clinic_id=CLINIC_ID,
            enrollment_id=enrollment_id,
            purpose="appointment_reminder",
            channel="email",
        )
        publish_configuration(
            clinic_id=CLINIC_ID,
            expected_version=0,
            content=ConfigurationContent(
                display_name="Synthetic Recovery Clinic",
                contact_email="clinic@example.invalid",
            ),
        )
        questionnaire = publish_template(
            clinic_id=CLINIC_ID,
            key="recovery-intake",
            title="Synthetic intake",
            questions=[
                {
                    "id": "q_notes",
                    "label": "Notes",
                    "type": "text",
                    "required": True,
                    "max_length": 4000,
                    "options": [],
                }
            ],
        )
        publish_specialty_template(
            clinic_id=CLINIC_ID,
            key="recovery-soap",
            title="Synthetic SOAP",
            prompts={
                "subjective": "S",
                "objective": "O",
                "assessment": "A",
                "plan": "P",
            },
        )
        consent_text = publish_text(
            clinic_id=CLINIC_ID,
            purpose="teleconsultation",
            text="Termo sintético de teleconsulta para ensaio de restauração.",
        )
        policy = propose_policy(
            clinic_id=CLINIC_ID,
            record_class="ehr.encounter",
            retention_days=3650,
        )
        approve_policy(clinic_id=CLINIC_ID, policy_id=policy.pk)
        invitation = issue_invitation(clinic_id=CLINIC_ID, enrollment_id=enrollment_id)
        add_waitlist_entry(
            clinic_id=CLINIC_ID,
            enrollment_id=enrollment_id,
            practitioner_id=PHYSICIAN_ID,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
        )
        issue_waitlist_offer(
            clinic_id=CLINIC_ID,
            practitioner_id=PHYSICIAN_ID,
            start_local="2035-06-02T10:30",
            end_local="2035-06-02T11:00",
        )

    with _runtime_role(), tenant_context(PHYSICIAN_ID, ORGANIZATION_ID):
        save_ui_preferences(
            theme=RESTORED_PREFERENCES[0], density=RESTORED_PREFERENCES[1]
        )
        response = assign_questionnaire(
            clinic_id=CLINIC_ID,
            enrollment_id=enrollment_id,
            template_id=questionnaire.pk,
        )

    with _runtime_role():
        session_id = redeem_invitation(CLINIC_ID, invitation.secret)
    if session_id is None:
        _fail("synthetic invitation redemption failed")
    with _runtime_role(), patient_session_context(session_id):
        submit_intake(
            response_id=response.pk,
            answers={"q_notes": "synthetic intake answer"},
            expected_revision=response.revision,
        )
        _text, offer = prepare_acceptance(text_id=consent_text.pk)
        record_consent(offer=offer, purpose="teleconsultation", accepted=True)
        slots = patient_slots(date(2035, 6, 2))
        if not slots:
            _fail("synthetic patient slots are unavailable")
        appointment = book_patient_slot(token=slots[0].token, idempotency_key=uuid4())

    request = _verified_request(PHYSICIAN_ID)
    with _runtime_role(), tenant_context(PHYSICIAN_ID, ORGANIZATION_ID):
        encounter = open_encounter(clinic_id=CLINIC_ID, appointment_id=appointment.pk)
        from apps.ehr.models import SpecialtyTemplate

        specialty = SpecialtyTemplate.objects.get(clinic_id=CLINIC_ID)
        version = create_draft(
            clinic_id=CLINIC_ID,
            encounter_id=encounter.pk,
            template_id=specialty.pk,
        )
        version = record_clinical_note(
            clinic_id=CLINIC_ID,
            version_id=version.pk,
            expected_revision=version.revision,
            content={
                "subjective": "synthetic subjective",
                "objective": "synthetic objective",
                "assessment": "synthetic assessment",
                "plan": "synthetic plan",
            },
        )
        finalized = finalize_version(
            clinic_id=CLINIC_ID,
            version_id=version.pk,
            expected_revision=version.revision,
            request=request,
        )
        amendment = amend_document(
            clinic_id=CLINIC_ID,
            version_id=finalized.pk,
            reason="synthetic amendment",
        )
        discard_draft(clinic_id=CLINIC_ID, version_id=amendment.pk)
        save_history(
            clinic_id=CLINIC_ID,
            encounter_id=encounter.pk,
            change=HistoryChange(
                kind="problem",
                expected_revision=0,
                state="documented",
                description="Problema sintético",
                status="active",
                reason="Registro inicial",
            ),
        )
        save_history(
            clinic_id=CLINIC_ID,
            encounter_id=encounter.pk,
            change=HistoryChange(
                kind="allergy",
                expected_revision=0,
                state="documented",
                description="Alergia sintética",
                status="active",
                reason="Registro inicial",
            ),
        )
        attachment = upload_attachment(
            clinic_id=CLINIC_ID,
            encounter_id=encounter.pk,
            upload=AttachmentInput(
                file_name="exame-sintetico.pdf",
                declared_type="application/pdf",
                data=PDF_BYTES,
            ),
        )
        scan_attachment(clinic_id=CLINIC_ID, attachment_id=attachment.pk)
        session = create_session(clinic_id=CLINIC_ID, encounter_id=encounter.pk)

    # The committed outbox row is executed through the real worker boundary;
    # eager Celery dispatch may already have run it, and the call is
    # idempotent either way.
    from apps.core.integration import execute_operation
    from apps.teleconsult.models import TeleconsultRoom

    with _owner_scope(ORGANIZATION_ID):
        room = TeleconsultRoom.objects.get(session_id=session.pk)
    with _runtime_role():
        execute_operation(room.operation_id)

    with _runtime_role(), tenant_context(PHYSICIAN_ID, ORGANIZATION_ID):
        physician_credential = request_physician_join(
            clinic_id=CLINIC_ID, session_id=session.pk
        )
        enter_room(token=physician_credential.token, role="physician")
        start_consultation(clinic_id=CLINIC_ID, session_id=session.pk)
        rx_draft = create_rx_draft(
            clinic_id=CLINIC_ID,
            encounter_id=encounter.pk,
            patient_id=patient_id,
            issuer_id=PHYSICIAN_ID,
            category=SYNTHETIC_CATEGORY,
        )
        save_draft(
            clinic_id=CLINIC_ID,
            draft_id=rx_draft.pk,
            encounter_id=encounter.pk,
            patient_id=patient_id,
            issuer_id=PHYSICIAN_ID,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[
                {
                    "medication_description": "Medicamento fictício",
                    "strength_form": "Concentração sintética",
                    "dose": "Dose sintética",
                    "route": "Via sintética",
                    "frequency": "Frequência sintética",
                    "duration": "Duração sintética",
                    "quantity": "Quantidade sintética",
                    "instructions": "Instruções sintéticas.",
                }
            ],
        )
        document = render_document(
            clinic_id=CLINIC_ID,
            draft_id=rx_draft.pk,
            encounter_id=encounter.pk,
            patient_id=patient_id,
            issuer_id=PHYSICIAN_ID,
            expected_version=2,
        )
        operation = request_signature(
            request=request,
            clinic_id=CLINIC_ID,
            document_id=document.pk,
        )

    provider = SyntheticSignatureProvider()
    with _owner_scope(ORGANIZATION_ID):
        operation.refresh_from_db()
    sign_request = SignatureRequest(
        operation_id=operation.pk,
        issuer_id=operation.issuer_id,
        signer_subject=operation.signer_subject,
        content_digest=operation.content_digest,
        content=bytes(document.pdf_bytes),
    )
    headers, body = provider.sign(sign_request, operation.operation_id)
    with _runtime_role():
        outcome = receive_signature_callback(
            provider=SYNTHETIC_PROVIDER, headers=headers, body=body
        )
    if outcome != "applied":
        _fail("synthetic signature callback was not applied")

    with _runtime_role(), tenant_context(PHYSICIAN_ID, ORGANIZATION_ID):
        release_document(clinic_id=CLINIC_ID, document_id=document.pk)
        revoke_document(
            clinic_id=CLINIC_ID,
            document_id=document.pk,
            reason=PrescriptionDocumentRevocation.Reason.ISSUER_REQUEST,
        )
        release_version(clinic_id=CLINIC_ID, version_id=finalized.pk)
        export_staff_records(clinic_id=CLINIC_ID, patient_id=patient_id)

    with _runtime_role(), patient_session_context(session_id):
        patient_credential = request_patient_join(session_id=session.pk)
        enter_room(token=patient_credential.token, role="patient")

    with _runtime_role(), tenant_context(PHYSICIAN_ID, ORGANIZATION_ID):
        end_consultation(clinic_id=CLINIC_ID, session_id=session.pk)
        discard_rx_draft(
            clinic_id=CLINIC_ID,
            draft_id=rx_draft.pk,
            encounter_id=encounter.pk,
            patient_id=patient_id,
            issuer_id=PHYSICIAN_ID,
            expected_version=2,
        )
        close_encounter(clinic_id=CLINIC_ID, encounter_id=encounter.pk)

    with _runtime_role():
        verify_handle(handle=document.qr_handle, remote_addr="127.0.0.1")

    with _runtime_role(), tenant_context(OWNER_ID, ORGANIZATION_ID):
        invoice = create_invoice(
            clinic_id=CLINIC_ID,
            patient_id=patient_id,
            amount_minor=15000,
            idempotency_key=uuid4(),
            appointment_id=appointment.pk,
            encounter_id=encounter.pk,
        )
        invoice = issue_invoice(
            clinic_id=CLINIC_ID,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )
        release_invoice(clinic_id=CLINIC_ID, invoice_id=invoice.pk)
        pix_operation = prepare_pix_charge(clinic_id=CLINIC_ID, invoice_id=invoice.pk)
        charge = complete_pix_charge(
            clinic_id=CLINIC_ID,
            invoice_id=invoice.pk,
            operation_id=pix_operation.pk,
        )
        place_hold(
            clinic_id=CLINIC_ID,
            record_class="ehr.encounter",
            record_id=encounter.pk,
            authority="synthetic-authority",
            reason="synthetic legal hold",
        )
        record_disposition(
            clinic_id=CLINIC_ID,
            record_class="ehr.encounter",
            record_id=encounter.pk,
        )

    register_payment_event_adapter(SyntheticPixAdapter())
    event_body = json.dumps(
        {
            "event_id": f"evt-{uuid4().hex[:12]}",
            "provider_reference": charge.provider_reference,
            "status": "pending",
            "amount_minor": 15000,
            "currency": "BRL",
        },
        separators=(",", ":"),
    ).encode()
    secret = os.environ.get("BILLING_SYNTHETIC_PIX_SECRET", "")
    signature = hmac.new(secret.encode(), event_body, hashlib.sha256).hexdigest()
    with _runtime_role():
        receive_payment_event(
            provider=PIX_PROVIDER,
            headers={SYNTHETIC_SIGNATURE_HEADER: signature},
            body=event_body,
        )

    kek = secret_store().get_secret("tenant-kek")
    with _owner_tenant_scope(ORGANIZATION_ID, OWNER_ID):
        envelope = encrypt(purpose=PROBE_PURPOSE, plaintext=PROBE_PLAINTEXT)
    probe = {
        "attachment_id": str(attachment.pk),
        "clinic_id": str(CLINIC_ID),
        "envelope_hex": envelope.hex(),
        "expected_sha256": hashlib.sha256(PROBE_PLAINTEXT).hexdigest(),
        "invoice_id": str(invoice.pk),
        "kek": kek,
        "organization_id": str(ORGANIZATION_ID),
        "owner_id": str(OWNER_ID),
        "patient_name": "Synthetic Recovery Persona",
        "patient_session_id": str(session_id),
        "physician_id": str(PHYSICIAN_ID),
        "version_id": str(finalized.pk),
    }
    descriptor = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(
            descriptor,
            json.dumps(probe, separators=(",", ":"), sort_keys=True).encode(),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify(probe_path: Path) -> None:
    """Exercise restored application workflows as ``clinic_app`` only.

    This operation runs against the restored target database through the
    runtime role: the registry search decrypts inside the database boundary,
    the clinical version and attachment download decrypt through the
    protected boundary, the patient charge resolver runs under the restored
    patient session, and the audit chain is recomputed row by row. Any
    drift in restored data, keys, policies or ACLs fails closed.
    """
    from apps.audit.verification import verify_chain_on_connection
    from apps.billing.presentation import patient_charge
    from apps.ehr.attachments import download_attachment
    from apps.ehr.services import view_version
    from apps.identity.preferences import load_ui_preferences
    from apps.intake.patient_access import (
        patient_session_context,
        patient_session_overview,
    )
    from apps.intake.patient_search import search_patients
    from apps.tenancy.db import tenant_context

    probe = _read_probe(probe_path)
    organization_id = UUID(probe["organization_id"])
    clinic_id = UUID(probe["clinic_id"])
    physician_id = UUID(probe["physician_id"])
    owner_id = UUID(probe["owner_id"])
    with _runtime_role(), tenant_context(owner_id, organization_id):
        page = search_patients(
            clinic_id=clinic_id,
            query=probe["patient_name"],
            birth_date=None,
            page=1,
        )
        if page.total != 1 or page.items[0].full_name != probe["patient_name"]:
            _fail("restored patient registry search failed")
    with _runtime_role(), tenant_context(physician_id, organization_id):
        version = view_version(
            clinic_id=clinic_id, version_id=UUID(probe["version_id"])
        )
        if version.subjective != "synthetic subjective":
            _fail("restored clinical content did not decrypt")
        download = download_attachment(
            clinic_id=clinic_id, attachment_id=UUID(probe["attachment_id"])
        )
        if download.data != PDF_BYTES:
            _fail("restored attachment bytes did not decrypt")
        preferences = load_ui_preferences()
        if (preferences.theme, preferences.density) != RESTORED_PREFERENCES:
            _fail("restored display preferences did not load")
    session_id = UUID(probe["patient_session_id"])
    with _runtime_role(), patient_session_context(session_id):
        overview = patient_session_overview()
        if overview is None or overview.patient_name != probe["patient_name"]:
            _fail("restored patient overview did not decrypt the name")
        charge = patient_charge(invoice_id=UUID(probe["invoice_id"]))
        if charge is None or charge.charge.instructions is None:
            _fail("restored patient charge resolver failed")
        if not charge.charge.instructions.copy_code.startswith(
            "SYNTHETIC-NOT-PAYABLE|"
        ):
            _fail("restored payment instructions did not decrypt")
    with _runtime_role(), tenant_context(physician_id, organization_id):
        result = verify_chain_on_connection(connection, organization_id)
        if result.row_count < 1:
            _fail("restored audit chain is empty")


def _read_probe(path: Path) -> dict[str, str]:
    """Read the seed-published probe binding for the verify step."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        _fail("key probe binding path is invalid")
    try:
        value: object = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"key probe binding is invalid: {error}")
    if not isinstance(value, dict):
        _fail("key probe binding is invalid")
    fields: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            _fail("key probe binding is invalid")
        fields[key] = item
    required = {
        "attachment_id",
        "clinic_id",
        "envelope_hex",
        "expected_sha256",
        "invoice_id",
        "kek",
        "organization_id",
        "owner_id",
        "patient_name",
        "patient_session_id",
        "physician_id",
        "version_id",
    }
    if not required <= set(fields):
        _fail("key probe binding is invalid")
    return fields


def main() -> int:  # noqa: C901 - one flat argument dispatch
    """Dispatch only the bootstrap, app-role TOTP, seed, or verify step."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("bootstrap", "totp", "seed", "verify"))
    parser.add_argument("--password-fd", type=int)
    parser.add_argument("--probe-path")
    arguments = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()
    if arguments.operation == "verify":
        if arguments.password_fd is not None or arguments.probe_path is None:
            _fail("verify requires exactly --probe-path")
        verify(Path(arguments.probe_path))
        return 0
    if arguments.operation == "totp":
        if arguments.password_fd is not None or arguments.probe_path is not None:
            _fail("TOTP fixture received unexpected arguments")
        create_totp()
        return 0
    if arguments.operation == "seed":
        if arguments.password_fd is not None or not arguments.probe_path:
            _fail("seed fixture requires exactly a probe path")
        seed(Path(arguments.probe_path))
        return 0
    if arguments.password_fd is None or arguments.password_fd < MIN_PRIVATE_FD:
        _fail("bootstrap fixture requires a private password descriptor")
    if arguments.probe_path is not None:
        _fail("bootstrap fixture received an unexpected probe path")
    password = bytearray(os.read(arguments.password_fd, MAX_PASSWORD_BYTES + 1))
    try:
        if (
            not password
            or len(password) > MAX_PASSWORD_BYTES
            or not password.endswith(b"\n")
        ):
            _fail("bootstrap password frame rejected")
        bootstrap(password[:-1].decode("utf-8"))
    finally:
        for index in range(len(password)):
            password[index] = 0
    return 0


def _fail(reason: str) -> Never:
    raise RuntimeError(reason)


if __name__ == "__main__":
    raise SystemExit(main())
