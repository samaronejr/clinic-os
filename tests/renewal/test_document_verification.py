"""Privacy-limited verification and delivery acceptance against real RLS."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.core.integration import execute_operation
from apps.core.secrets import secret_store
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.models import PhysicianProfile
from apps.intake.models import PatientContact
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    issue_invitation,
    patient_session_context,
)
from apps.prescription import signature_provider as provider_module
from apps.prescription import signing
from apps.prescription.models import (
    PrescriptionDocument,
    PrescriptionDocumentRevocation,
    SignatureOperation,
    VerificationProbe,
)
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.prescription.services import save_draft
from apps.prescription.signature_provider import (
    SIGNATURE_HEADER,
    SYNTHETIC_PROVIDER,
    SignatureRequest,
    SyntheticSignatureProvider,
)
from apps.prescription.signing import request_signature
from apps.prescription.verification import (
    VERIFY_MAX_LOOKUPS,
    VerificationLimitedError,
    VerificationResult,
    deliver_document,
    patient_document_download,
    patient_documents,
    release_document,
    revoke_document,
    revoke_document_release,
    verify_handle,
)
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import KEK_SECRET_NAME
from django.db import DatabaseError, connection, transaction
from django.test import Client, override_settings
from django.utils import timezone

from patient_service_support import runtime_role
from renewal.test_document_artifacts import ITEM, Scope, seed_rendered
from renewal.test_encounters import physician_client, setup_context
from stepup_test_support import verified_request

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.http import HttpRequest
    from pytest_django.fixtures import SettingsWrapper

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _kek() -> str:
    """The resolver decrypts in its own boundary; callers pass the KEK."""
    return secret_store().get_secret(KEK_SECRET_NAME)


@contextmanager
def owner_scope(organization_id: UUID) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        yield


@dataclass
class SignedSetup:
    """One rendered document plus the issuer profile and step-up request."""

    graph: RbacGraph
    scope: Scope
    document: PrescriptionDocument
    request: HttpRequest
    provider: SyntheticSignatureProvider

    def issue(self) -> SignatureOperation:
        """Run the real synthetic lifecycle to rehearsal_complete."""
        with (
            runtime_role(),
            tenant_context(self.graph.physician, self.graph.organization_a),
        ):
            operation = request_signature(
                request=self.request,
                clinic_id=self.graph.clinic_a,
                document_id=self.document.pk,
            )
        with owner_scope(self.graph.organization_a):
            operation.refresh_from_db()
        assert operation.state == "signing"
        request = SignatureRequest(
            operation_id=operation.pk,
            issuer_id=operation.issuer_id,
            signer_subject=operation.signer_subject,
            content_digest=operation.content_digest,
            content=bytes(self.document.pdf_bytes),
        )
        headers, body = self.provider.sign(request, operation.operation_id)
        with runtime_role():
            result = signing.receive_signature_callback(
                provider=SYNTHETIC_PROVIDER, headers=headers, body=body
            )
        assert result == "applied"
        with owner_scope(self.graph.organization_a):
            operation.refresh_from_db()
        assert operation.state == "rehearsal_complete"
        return operation

    def amend_and_issue(self) -> PrescriptionDocument:
        """Save a new draft version, render and sign the superseding doc."""
        from apps.prescription.services import render_document  # noqa: PLC0415

        with (
            runtime_role(),
            tenant_context(self.graph.physician, self.graph.organization_a),
        ):
            draft = save_draft(
                **self.scope,
                draft_id=self.document.draft_id,
                category=SYNTHETIC_CATEGORY,
                expected_version=self.document.document_version,
                items=[{**ITEM, "dose": "Dose substituída"}],
            )
            newer = render_document(
                clinic_id=self.graph.clinic_a,
                draft_id=draft.pk,
                encounter_id=self.scope["encounter_id"],
                patient_id=self.scope["patient_id"],
                issuer_id=self.scope["issuer_id"],
                expected_version=draft.version,
            )
        document = SignedSetup(
            self.graph, self.scope, newer, self.request, self.provider
        )
        document.issue()
        return newer


@pytest.fixture
def signed(rbac_graph: RbacGraph) -> Iterator[SignedSetup]:
    """Seed one rendered document and complete the synthetic signature."""
    scope, document = seed_rendered(rbac_graph)
    with owner_scope(rbac_graph.organization_a):
        PhysicianProfile.objects.create(
            organization_id=rbac_graph.organization_a,
            user_id=rbac_graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-35",
            signing_subject=f"synthetic:physician:{rbac_graph.physician}",
        )
    request = verified_request(rbac_graph.physician)
    with override_settings(
        PHYSICIAN_SYNTHETIC_REGISTRY=True, PRESCRIPTION_SYNTHETIC_SIGNING=True
    ):
        yield SignedSetup(
            rbac_graph, scope, document, request, SyntheticSignatureProvider()
        )


def _verify(handle: object, addr: str = "198.51.100.7") -> VerificationResult:
    """Run the public lookup as the runtime role, like the real request."""
    with runtime_role():
        return verify_handle(handle=handle, remote_addr=addr)


def _patient_session(graph: RbacGraph, patient_id: UUID) -> Client:
    """Mint a real patient session through the redemption boundary."""
    with setup_context(graph.organization_a):
        from apps.intake.models import (  # noqa: PLC0415
            PatientClinicEnrollment,
        )

        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=patient_id, clinic_id=graph.clinic_a
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    patient = Client()
    with runtime_role():
        redeemed = patient.post(
            f"/patient/access/{graph.clinic_a}/", {"code": invitation.secret}
        )
        assert redeemed.status_code == 302
    return patient


def _second_patient(graph: RbacGraph) -> UUID:
    """Enroll a second patient in clinic A through the real service."""
    import datetime as dt  # noqa: PLC0415

    from apps.intake.services import create_patient  # noqa: PLC0415

    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        created = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Outra Paciente Sintética",
            birth_date=dt.date(1988, 4, 2),
            idempotency_key=uuid4(),
        )
    return created.patient.pk


def test_issued_document_verifies_with_minimal_public_result(
    signed: SignedSetup,
) -> None:
    operation = signed.issue()
    result = _verify(signed.document.qr_handle)
    assert result.status == "rehearsal_complete"
    assert result.document_version == signed.document.document_version
    assert result.issued_at == operation.completed_at
    assert result.content_digest == signed.document.pdf_digest
    assert result.signed_digest == operation.signed_digest
    assert result.issuer_label == signed.document.frozen_input["issuer_label"]
    assert result.clinic_label == signed.document.frozen_input["clinic_label"]


def test_absent_malformed_and_unissued_handles_are_indistinguishable(
    signed: SignedSetup,
) -> None:
    # The rendered-but-unsigned document, a random handle, a malformed
    # handle and a non-string all return the same minimal unavailable result.
    unissued = _verify(signed.document.qr_handle)
    unknown = _verify("a" * 43)
    malformed = _verify("has spaces and !!!! padding")
    non_string = _verify(None)
    for result in (unissued, unknown, malformed, non_string):
        assert result.status == "unavailable"
        assert result.document_version is None
        assert result.issued_at is None
        assert result.content_digest is None
        assert result.signed_digest is None
        assert result.issuer_label is None
        assert result.clinic_label is None


def test_anonymous_lookup_is_rate_limited_per_probe(signed: SignedSetup) -> None:
    for _ in range(VERIFY_MAX_LOOKUPS):
        assert _verify("b" * 43, addr="203.0.113.9").status == "unavailable"
    with pytest.raises(VerificationLimitedError):
        _verify("b" * 43, addr="203.0.113.9")
    # A different probe keeps its own allowance.
    assert _verify("b" * 43, addr="203.0.113.10").status == "unavailable"
    # The runtime role holds no grant on the probe table.
    with (
        owner_scope(signed.graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        VerificationProbe.objects.create(
            probe_key=b"\x00" * 32,
            window_start="2030-01-01T00:00:00Z",
            lookups=0,
        )
    with runtime_role():
        with pytest.raises(DatabaseError), transaction.atomic():
            VerificationProbe.objects.create(
                probe_key=b"\x01" * 32,
                window_start="2030-01-01T00:00:00Z",
                lookups=0,
            )
        # The runtime role cannot even read the probe counters.
        with pytest.raises(DatabaseError), transaction.atomic():
            VerificationProbe.objects.exists()
    with owner_scope(signed.graph.organization_a):
        # FORCE RLS without an owner policy hides every probe row.
        assert not VerificationProbe.objects.exists()


def test_qr_handle_never_grants_download_access(signed: SignedSetup) -> None:
    signed.issue()
    handle = signed.document.qr_handle
    client = Client()
    with runtime_role():
        # The public route returns a status page, never bytes.
        page = client.get(f"/prescription/verify/{handle}/")
        assert page.status_code == 200
        assert page.headers["Content-Type"].startswith("text/html")
        assert "no-store" in page.headers["Cache-Control"]
        content = page.content.decode()
        assert 'data-status="rehearsal_complete"' in content
        patient_label = signed.document.frozen_input["patient_label"]
        assert patient_label not in content
        assert ITEM["medication_description"].strip() not in content
        assert str(signed.scope["patient_id"]) not in content
        # A well-formed unknown handle resolves to the same minimal page.
        unknown = client.get(f"/prescription/verify/{uuid4().hex + 'a' * 11}/")
        assert unknown.status_code == 200
        assert 'data-status="unavailable"' in unknown.content.decode()
        assert (
            client.post(
                "/patient/documents/",
                {"action": "download", "document_id": handle},
            ).status_code
            == 403
        )


def test_resolver_returns_no_bytes_to_unauthorized_runtime(
    signed: SignedSetup,
) -> None:
    """The QR handle alone must never yield document bytes to clinic_app.

    Direct resolver calls as the runtime role, with tenant, actor and
    patient-session context cleared: the public resolver's declared
    projection has no bytea column, its row carries no bytes, and the
    patient byte resolver stays NULL without a session and under a
    different patient's session.
    """
    operation = signed.issue()
    graph = signed.graph
    with runtime_role(), connection.cursor() as cursor:
        for key in (
            "app.current_tenant",
            "app.current_user_id",
            "app.current_patient_session",
        ):
            cursor.execute("SELECT set_config(%s, %s, false)", [key, ""])
        cursor.execute("SELECT current_user")
        assert cursor.fetchone()[0] == "clinic_app"
        # RLS hides the document row itself; the handle is the only input.
        assert not PrescriptionDocument.objects.filter(pk=signed.document.pk).exists()
        # The resolver's declared projection is byte-free by contract.
        cursor.execute(
            "SELECT pg_catalog.pg_get_function_result("
            "'clinic_app.prescription_verify(text, text, boolean)'"
            "::pg_catalog.regprocedure)"
        )
        assert "bytea" not in cursor.fetchone()[0]
        cursor.execute(
            "SELECT * FROM clinic_app.prescription_verify(%s, %s, %s)",
            [_kek(), signed.document.qr_handle, True],
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "rehearsal_complete"
        assert len(row) == 7
        for value in row:
            assert not isinstance(value, (bytes, bytearray, memoryview))
        assert bytes(operation.signed_bytes) not in row
        # The patient byte resolver stays NULL without a session.
        cursor.execute(
            "SELECT clinic_app.prescription_patient_document_bytes(%s, %s)",
            [_kek(), str(signed.document.pk)],
        )
        assert cursor.fetchone()[0] is None
    result = _verify(signed.document.qr_handle)
    assert result.status == "rehearsal_complete"
    for value in vars(result).values():
        assert not isinstance(value, (bytes, bytearray, memoryview))
    # A different patient's live session is still not this document's
    # release authority: the byte resolver returns NULL and the service
    # layer denies.
    other = _patient_session(graph, _second_patient(graph))
    other_session = UUID(str(other.session[PATIENT_SESSION_KEY]))
    with runtime_role(), patient_session_context(other_session):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.prescription_patient_document_bytes(%s, %s)",
                [_kek(), str(signed.document.pk)],
            )
            assert cursor.fetchone()[0] is None
        assert patient_documents() == []
        with pytest.raises(ClinicalAccessDeniedError):
            patient_document_download(document_id=signed.document.pk)


def test_revocation_and_supersession_publish_without_rewriting_bytes(
    signed: SignedSetup,
) -> None:
    operation = signed.issue()
    original_pdf = bytes(signed.document.pdf_bytes)
    original_signed = bytes(operation.signed_bytes)
    with (
        runtime_role(),
        tenant_context(signed.graph.physician, signed.graph.organization_a),
    ):
        revoke_document(
            clinic_id=signed.graph.clinic_a,
            document_id=signed.document.pk,
            reason="issuer_request",
        )
    revoked = _verify(signed.document.qr_handle)
    assert revoked.status == "revoked"
    assert revoked.document_version == signed.document.document_version
    assert revoked.signed_digest == operation.signed_digest
    # A second revocation returns the stored row; nothing is rewritten.
    with (
        runtime_role(),
        tenant_context(signed.graph.physician, signed.graph.organization_a),
    ):
        again = revoke_document(
            clinic_id=signed.graph.clinic_a,
            document_id=signed.document.pk,
            reason="clinical_error",
        )
        assert again.reason == "issuer_request"
        stored = PrescriptionDocument.objects.get(pk=signed.document.pk)
        assert bytes(stored.pdf_bytes) == original_pdf
        stored_op = SignatureOperation.objects.get(pk=operation.pk)
        assert bytes(stored_op.signed_bytes) == original_signed
        # The revocation row is insert-only.
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionDocumentRevocation.objects.filter(pk=again.pk).update(
                reason="clinical_error"
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionDocumentRevocation.objects.filter(pk=again.pk).delete()


def test_superseded_status_distinguishes_replaced_documents(
    signed: SignedSetup,
) -> None:
    signed.issue()
    newer = signed.amend_and_issue()
    superseded = _verify(signed.document.qr_handle)
    assert superseded.status == "superseded"
    assert superseded.signed_digest is not None
    current = _verify(newer.qr_handle)
    assert current.status == "rehearsal_complete"
    assert current.document_version == newer.document_version


def _overwrite_signed_bytes(
    signed: SignedSetup, operation_id: UUID, payload: bytes
) -> None:
    """Write raw stored bytes past the immutable signature trigger."""
    with setup_context(signed.graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE clinic_app.prescription_signatureoperation "
            "DISABLE TRIGGER prescription_signature_guard"
        )
        try:
            cursor.execute(
                "UPDATE clinic_app.prescription_signatureoperation "
                "SET signed_bytes = %s WHERE id = %s",
                [payload, str(operation_id)],
            )
        finally:
            cursor.execute(
                "ALTER TABLE clinic_app.prescription_signatureoperation "
                "ENABLE TRIGGER prescription_signature_guard"
            )


def test_tampered_signed_bytes_and_disabled_verifier_fail_closed(
    signed: SignedSetup,
) -> None:
    operation = signed.issue()
    # The column holds a tenant envelope; corrupt the stored envelope past
    # the immutable trigger, as a storage-layer failure would, and keep the
    # original bytes to restore afterwards.
    with setup_context(signed.graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT signed_bytes FROM clinic_app.prescription_signatureoperation "
            "WHERE id = %s",
            [str(operation.pk)],
        )
        stored_envelope = bytes(cursor.fetchone()[0])
    assert stored_envelope != bytes(operation.signed_bytes)
    _overwrite_signed_bytes(signed, operation.pk, stored_envelope + b"tampered")
    tampered = _verify(signed.document.qr_handle)
    assert tampered.status == "invalid"
    assert tampered.document_version == signed.document.document_version
    assert tampered.signed_digest is None
    # Restore the real envelope; a disabled verifier reports unavailable,
    # never a false valid status.
    _overwrite_signed_bytes(signed, operation.pk, stored_envelope)
    with override_settings(PRESCRIPTION_SYNTHETIC_SIGNING=False):
        assert _verify(signed.document.qr_handle).status == "unavailable"
    assert _verify(signed.document.qr_handle).status == "rehearsal_complete"


def test_release_gates_patient_download_and_revocation_restores_denial(
    signed: SignedSetup,
) -> None:
    operation = signed.issue()
    graph = signed.graph
    patient = _patient_session(graph, signed.scope["patient_id"])
    with runtime_role():
        # Nothing is visible before the issuer releases the document.
        assert patient.get("/patient/documents/").content.find(b"documents-empty") != -1
        denied = patient.post(
            "/patient/documents/",
            {"action": "download", "document_id": str(signed.document.pk)},
        )
        assert denied.status_code == 403
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        release = release_document(
            clinic_id=graph.clinic_a, document_id=signed.document.pk
        )
        # Re-releasing is idempotent.
        assert (
            release_document(
                clinic_id=graph.clinic_a, document_id=signed.document.pk
            ).pk
            == release.pk
        )
    session_id = UUID(str(patient.session[PATIENT_SESSION_KEY]))
    with runtime_role(), patient_session_context(session_id):
        documents = patient_documents()
        assert len(documents) == 1
        assert documents[0].document_id == signed.document.pk
        download = patient_document_download(document_id=signed.document.pk)
        assert download.data == bytes(operation.signed_bytes)
    # Session minting resets the runtime role, so it stays outside.
    other_patient = _patient_session(graph, _second_patient(graph))
    with runtime_role():
        page = patient.get("/patient/documents/")
        assert page.status_code == 200
        assert f'data-document="{signed.document.pk}"' in page.content.decode()
        fetched = patient.post(
            "/patient/documents/",
            {"action": "download", "document_id": str(signed.document.pk)},
        )
        assert fetched.status_code == 200
        assert fetched.content == bytes(operation.signed_bytes)
        assert "no-store" in fetched.headers["Cache-Control"]
        # Another patient's session sees nothing.
        other_page = other_patient.get("/patient/documents/")
        assert other_page.status_code == 200
        assert b"documents-empty" in other_page.content
        assert (
            other_patient.post(
                "/patient/documents/",
                {"action": "download", "document_id": str(signed.document.pk)},
            ).status_code
            == 403
        )
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        revoke_document_release(
            clinic_id=graph.clinic_a, document_id=signed.document.pk
        )
    with runtime_role(), patient_session_context(session_id):
        with pytest.raises(ClinicalAccessDeniedError):
            patient_document_download(document_id=signed.document.pk)
        assert patient_documents() == []
    with runtime_role():
        assert (
            patient.post(
                "/patient/documents/",
                {"action": "download", "document_id": str(signed.document.pk)},
            ).status_code
            == 403
        )


def test_release_requires_completed_signature_and_issuer(
    signed: SignedSetup,
) -> None:
    graph = signed.graph
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        # An unsigned document cannot be released or revoked.
        with pytest.raises(ClinicalConflictError, match="not_issued"):
            release_document(clinic_id=graph.clinic_a, document_id=signed.document.pk)
        with pytest.raises(ClinicalConflictError, match="not_issued"):
            revoke_document(
                clinic_id=graph.clinic_a,
                document_id=signed.document.pk,
                reason="issuer_request",
            )
    signed.issue()
    # A same-clinic non-issuer is denied.
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        release_document(clinic_id=graph.clinic_a, document_id=signed.document.pk)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        with pytest.raises(ClinicalAccessDeniedError):
            revoke_document(
                clinic_id=graph.clinic_a,
                document_id=signed.document.pk,
                reason="not_a_reason",
            )
        with pytest.raises(ClinicalAccessDeniedError):
            revoke_document_release(
                clinic_id=graph.clinic_a, document_id=signed.document.pk
            )


def test_delivery_uses_shared_outbox_with_link_only_message(
    signed: SignedSetup,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    graph = signed.graph
    signed.issue()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        release_document(clinic_id=graph.clinic_a, document_id=signed.document.pk)
        # Without a verified email contact, delivery fails closed.
        with pytest.raises(ClinicalConflictError, match="no_verified_contact"):
            deliver_document(clinic_id=graph.clinic_a, document_id=signed.document.pk)
        PatientContact.objects.create(
            organization_id=graph.organization_a,
            patient_id=signed.scope["patient_id"],
            channel="email",
            destination="paciente@synthetic.invalid",
            destination_version=1,
            verified_version=1,
            verified_at=timezone.now(),
            verification_method="synthetic",
        )
        dispatched: list[str] = []
        monkeypatch.setattr(
            execute_operation_task,
            "apply_async",
            lambda **kwargs: dispatched.append(kwargs["kwargs"]["operation_id"]),
        )
        operation_id = deliver_document(
            clinic_id=graph.clinic_a, document_id=signed.document.pk
        )
        operation = IntegrationOperation.objects.get(pk=operation_id)
        assert operation.channel == "email"
        assert operation.provider == "document-delivery-v1"
        assert operation.subject_type == "prescription.document"
        assert operation.subject_id == signed.document.pk
        assert operation.status == "pending"
    assert dispatched == [str(operation_id)]
    # The worker boundary sends a link-only message; no bytes attach.
    # Re-register the real adapter and recheck in case another test cleared
    # the integration registries.
    from apps.core.integration import (  # noqa: PLC0415
        register_send_adapter,
        register_subject_recheck,
    )
    from apps.prescription.delivery import (  # noqa: PLC0415
        DocumentDeliveryAdapter,
    )
    from apps.prescription.verification import (  # noqa: PLC0415
        DELIVERY_SUBJECT_TYPE,
        document_delivery_eligible,
        document_delivery_lock_key,
    )

    register_send_adapter(DocumentDeliveryAdapter())
    register_subject_recheck(
        DELIVERY_SUBJECT_TYPE,
        document_delivery_eligible,
        lock_key=document_delivery_lock_key,
    )
    settings.COMMS_SYNTHETIC_CHANNELS = ["email"]
    with runtime_role():
        assert execute_operation(operation_id) == "succeeded"
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        operation.refresh_from_db()
        assert operation.status == "succeeded"
        assert operation.provider_reference == f"synthetic:email:{operation_id}"
    # Revocation cancels a queued delivery through the send-time recheck.
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        revoke_document(
            clinic_id=graph.clinic_a,
            document_id=signed.document.pk,
            reason="issuer_request",
        )
        # A fresh release cannot resurrect the revoked document's delivery.
        with pytest.raises(ClinicalConflictError):
            deliver_document(clinic_id=graph.clinic_a, document_id=signed.document.pk)


def test_http_verify_and_patient_download_journey(signed: SignedSetup) -> None:
    graph = signed.graph
    operation = signed.issue()
    url = f"/prescription/clinics/{graph.clinic_a}/draft/"
    with physician_client(graph) as client:
        assert (
            client.post(
                url, {"action": "open", "encounter_id": signed.scope["encounter_id"]}
            ).status_code
            == 302
        )
        released = client.post(
            url,
            {
                "action": "release_document",
                "encounter_id": signed.scope["encounter_id"],
                "document_id": str(signed.document.pk),
            },
        )
        assert released.status_code == 302
    patient = _patient_session(graph, signed.scope["patient_id"])
    with runtime_role():
        page = patient.get("/patient/documents/")
        assert page.status_code == 200
        fetched = patient.post(
            "/patient/documents/",
            {"action": "download", "document_id": str(signed.document.pk)},
        )
        assert fetched.status_code == 200
        assert sha256(fetched.content).hexdigest() == operation.signed_digest
        # Anonymous verification stays minimal and no-store.
        anonymous = Client()
        verified = anonymous.get(f"/prescription/verify/{signed.document.qr_handle}/")
        assert verified.status_code == 200
        body = verified.content.decode()
        assert 'data-status="rehearsal_complete"' in body
        assert operation.signed_digest in body
        assert signed.document.frozen_input["patient_label"] not in body
        assert "no-store" in verified.headers["Cache-Control"]
        # The QR handle cannot reach the patient or staff download paths.
        assert (
            anonymous.post(
                "/patient/documents/",
                {"action": "download", "document_id": signed.document.qr_handle},
            ).status_code
            == 403
        )
        assert (
            anonymous.get(f"/prescription/clinics/{graph.clinic_a}/draft/").status_code
            == 403
        )


def test_callback_forgery_cannot_publish_status(signed: SignedSetup) -> None:
    """A forged callback never produces a verifiable status."""
    graph = signed.graph
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        operation = request_signature(
            request=signed.request,
            clinic_id=graph.clinic_a,
            document_id=signed.document.pk,
        )
    with owner_scope(graph.organization_a):
        operation.refresh_from_db()
    payload = {
        "event_id": "event-forged",
        "operation_id": operation.operation_id,
        "status": "signed",
        "signed_bytes": base64.b64encode(b"forged").decode("ascii"),
    }
    body = json.dumps(payload).encode()
    forged = {SIGNATURE_HEADER: "0" * 64}
    with runtime_role():
        from apps.prescription.signature_provider import (  # noqa: PLC0415
            SignatureCallbackError,
        )

        with pytest.raises(SignatureCallbackError):
            signing.receive_signature_callback(
                provider=SYNTHETIC_PROVIDER, headers=forged, body=body
            )
    assert _verify(signed.document.qr_handle).status == "unavailable"
    # A well-signed callback for an unknown operation is equally absent.
    unknown = json.dumps(
        {
            "event_id": "event-unknown",
            "operation_id": "synop-unknown",
            "status": "signed",
            "signed_bytes": base64.b64encode(b"x").decode("ascii"),
        }
    ).encode()
    headers = {
        SIGNATURE_HEADER: hmac.new(
            provider_module.SYNTHETIC_SECRET, unknown, hashlib.sha256
        ).hexdigest()
    }
    with runtime_role():
        assert (
            signing.receive_signature_callback(
                provider=SYNTHETIC_PROVIDER, headers=headers, body=unknown
            )
            == "rejected"
        )
    assert _verify(signed.document.qr_handle).status == "unavailable"
