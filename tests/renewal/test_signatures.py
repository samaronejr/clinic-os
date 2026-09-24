"""Signature lifecycle acceptance against real clinic_app RLS and triggers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity import physician_verification as verification
from apps.identity import stepup
from apps.identity.models import PhysicianEvidence, PhysicianProfile, UserClinicRole
from apps.identity.physician_registry import (
    RegistrationIdentity,
    RegistryResponse,
    SyntheticPhysicianRegistry,
)
from apps.identity.physician_verification import PhysicianVerificationRequired
from apps.identity.stepup import STEP_UP_SESSION_KEY, StepUpRequired
from apps.prescription import signature_provider as provider_module
from apps.prescription import signing
from apps.prescription.models import (
    PrescriptionDocument,
    SignatureCallback,
    SignatureOperation,
)
from apps.prescription.services import save_draft
from apps.prescription.signature_provider import (
    SIGNATURE_HEADER,
    SYNTHETIC_MARKER,
    SYNTHETIC_PROVIDER,
    SignatureAcceptance,
    SignatureCallbackError,
    SignatureProviderRejectedError,
    SignatureProviderTransientError,
    SignatureProviderUnavailableError,
    SignatureRequest,
    SignatureVerificationError,
    SyntheticSignatureProvider,
)
from apps.prescription.signing import (
    SignatureUnavailableError,
    abandon_signature,
    dispatch_signature,
    download_signed_document,
    initiate_signature,
    receive_signature_callback,
    request_signature,
    retry_dispatch,
    signature_status,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.test import override_settings

from patient_service_support import runtime_role
from renewal.test_document_artifacts import ITEM, Scope, seed_rendered
from renewal.test_encounters import physician_client
from stepup_test_support import verified_request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2036, 1, 1, tzinfo=UTC)


@contextmanager
def owner_scope(organization_id: UUID) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        yield


def _provider_for(name: str) -> SyntheticSignatureProvider:
    if name == SYNTHETIC_PROVIDER:
        return SyntheticSignatureProvider()
    raise SignatureProviderUnavailableError


def _unavailable_provider(name: str) -> SyntheticSignatureProvider:
    raise SignatureProviderUnavailableError


@dataclass
class SigningSetup:
    graph: RbacGraph
    scope: Scope
    document: PrescriptionDocument
    profile: PhysicianProfile
    request: HttpRequest
    provider: SyntheticSignatureProvider

    def request_signature(self) -> SignatureOperation:
        with (
            runtime_role(),
            tenant_context(self.graph.physician, self.graph.organization_a),
        ):
            operation = request_signature(
                request=self.request,
                clinic_id=self.graph.clinic_a,
                document_id=self.document.pk,
            )
        # Dispatch runs on commit; the returned row reflects post-commit state.
        return self.refresh(operation)

    def initiate(self) -> SignatureOperation:
        with (
            runtime_role(),
            tenant_context(self.graph.physician, self.graph.organization_a),
        ):
            return initiate_signature(
                clinic_id=self.graph.clinic_a, document_id=self.document.pk
            )

    def prepare(self, operation_id: UUID) -> None:
        with (
            runtime_role(),
            tenant_context(self.graph.physician, self.graph.organization_a),
        ):
            signing._prepare_signature(
                request=self.request,
                clinic_id=self.graph.clinic_a,
                operation_id=operation_id,
            )

    def dispatch(self, operation_id: UUID) -> str:
        with runtime_role():
            return dispatch_signature(operation_id)

    def callback(
        self,
        operation: SignatureOperation,
        *,
        event_id: str | None = None,
        signed_bytes: bytes | None = None,
        status: str = "signed",
    ) -> tuple[dict[str, str], bytes]:
        request = SignatureRequest(
            operation_id=operation.pk,
            issuer_id=operation.issuer_id,
            signer_subject=operation.signer_subject,
            content_digest=operation.content_digest,
            content=bytes(self.document.pdf_bytes),
        )
        if status == "signed":
            headers, body = self.provider.sign(request, operation.operation_id)
            if signed_bytes is not None or event_id is not None:
                payload = json.loads(body)
                if signed_bytes is not None:
                    payload["signed_bytes"] = base64.b64encode(signed_bytes).decode(
                        "ascii"
                    )
                if event_id is not None:
                    payload["event_id"] = event_id
                body = json.dumps(payload).encode()
                headers = {
                    SIGNATURE_HEADER: hmac.new(
                        provider_module.SYNTHETIC_SECRET, body, hashlib.sha256
                    ).hexdigest()
                }
            return headers, body
        payload = {
            "event_id": event_id or f"event-{uuid4().hex[:8]}",
            "operation_id": operation.operation_id,
            "status": "failed",
        }
        body = json.dumps(payload).encode()
        return {
            SIGNATURE_HEADER: hmac.new(
                provider_module.SYNTHETIC_SECRET, body, hashlib.sha256
            ).hexdigest()
        }, body

    def receive(
        self,
        headers: dict[str, str],
        body: bytes,
        *,
        provider: str = SYNTHETIC_PROVIDER,
    ) -> str:
        with runtime_role():
            return receive_signature_callback(
                provider=provider, headers=headers, body=body
            )

    def refresh(self, operation: SignatureOperation) -> SignatureOperation:
        with owner_scope(self.graph.organization_a):
            operation.refresh_from_db()
        return operation

    def issue(self) -> SignatureOperation:
        operation = self.request_signature()
        assert operation.state == "signing"
        headers, body = self.callback(operation)
        assert self.receive(headers, body) == "applied"
        return self.refresh(operation)


@pytest.fixture
def setup(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SigningSetup]:
    monkeypatch.setattr(verification, "utc_now", lambda: NOW)
    monkeypatch.setattr("apps.identity.physician_registry.utc_now", lambda: NOW)
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: int(NOW.timestamp()))
    monkeypatch.setattr(signing, "utc_now", lambda: NOW)
    monkeypatch.setattr(provider_module, "utc_now", lambda: NOW)
    monkeypatch.setattr(signing, "signature_provider_for", _provider_for)
    scope, document = seed_rendered(rbac_graph)
    with owner_scope(rbac_graph.organization_a):
        profile = PhysicianProfile.objects.create(
            organization_id=rbac_graph.organization_a,
            user_id=rbac_graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-34",
            signing_subject=f"synthetic:physician:{rbac_graph.physician}",
        )
    request = verified_request(rbac_graph.physician, verified_at=int(NOW.timestamp()))
    with override_settings(
        PHYSICIAN_SYNTHETIC_REGISTRY=True, PRESCRIPTION_SYNTHETIC_SIGNING=True
    ):
        yield SigningSetup(
            rbac_graph,
            scope,
            document,
            profile,
            request,
            SyntheticSignatureProvider(),
        )


def test_exact_byte_synthetic_lifecycle_completes_rehearsal_and_binds_evidence(
    setup: SigningSetup,
) -> None:
    operation = setup.issue()
    graph = setup.graph
    # A verified synthetic callback completes the rehearsal; it never issues.
    assert operation.state == "rehearsal_complete"
    assert operation.operation_id.startswith("synop-")
    assert operation.content_digest == setup.document.pdf_digest
    assert operation.signer_subject == setup.profile.signing_subject
    assert operation.evidence_id is not None
    snapshot = operation.evidence_snapshot
    assert snapshot is not None
    assert snapshot["reason_code"] == "verified_synthetic"
    assert snapshot["content_digest"] == setup.document.pdf_digest
    assert snapshot["document_version"] == setup.document.document_version
    assert snapshot["signing_subject"] == setup.profile.signing_subject
    # The authorization deadline is frozen at preparation: the earliest of
    # the step-up window (300s) and the evidence recheck bound (5 minutes).
    assert operation.authorized_until == NOW + timedelta(minutes=5)
    assert snapshot["authorized_until"] == (NOW + timedelta(minutes=5)).isoformat()
    # Completion time comes from validated signing evidence, not the clock.
    assert operation.completed_at == NOW
    assert operation.signed_digest == sha256(bytes(operation.signed_bytes)).hexdigest()
    signed = bytes(operation.signed_bytes)
    assert signed.startswith(bytes(setup.document.pdf_bytes))
    assert SYNTHETIC_MARKER.strip() in signed
    with owner_scope(graph.organization_a):
        callback = SignatureCallback.objects.get(operation_id=operation.pk)
        assert callback.payload["operation_id"] == operation.operation_id
        assert callback.verified_at == NOW
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        stored = SignatureOperation.objects.get(pk=operation.pk)
        assert stored.state == "rehearsal_complete"
        download = download_signed_document(
            clinic_id=graph.clinic_a, operation_id=operation.pk
        )
        assert download.data == signed
        assert download.content_type == "application/pdf"


def test_state_machine_rejects_illegal_and_terminal_transitions(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    operation = setup.initiate()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        for target in ("signing", "issued", "rehearsal_complete"):
            with pytest.raises(DatabaseError), transaction.atomic():
                SignatureOperation.objects.filter(pk=operation.pk).update(state=target)
        operation.refresh_from_db()
        assert operation.state == "draft"
    operation = setup.issue()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        for mutation in (
            {"state": "failed"},
            {"signed_bytes": b"forged"},
            {"content_digest": "0" * 64},
            {"signer_subject": "synthetic:forged"},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                SignatureOperation.objects.filter(pk=operation.pk).update(**mutation)
        operation.refresh_from_db()
        assert operation.state == "rehearsal_complete"
        with pytest.raises(DatabaseError), transaction.atomic():
            SignatureOperation.objects.filter(pk=operation.pk).delete()


def test_stale_step_up_blocks_prepare_without_registry_call(
    setup: SigningSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    class CountingRegistry(SyntheticPhysicianRegistry):
        def lookup(self, identity: RegistrationIdentity) -> RegistryResponse:
            nonlocal calls
            calls += 1
            return super().lookup(identity)

    monkeypatch.setattr(verification, "get_physician_registry", CountingRegistry)
    graph = setup.graph
    setup.request.session[STEP_UP_SESSION_KEY] = int(NOW.timestamp()) - 301
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(StepUpRequired):
            request_signature(
                request=setup.request,
                clinic_id=graph.clinic_a,
                document_id=setup.document.pk,
            )
        assert calls == 0
        operation = SignatureOperation.objects.get()
        assert operation.state == "draft"
        assert operation.evidence_id is None
        assert not PhysicianEvidence.objects.exists()


def _advance_clocks(monkeypatch: pytest.MonkeyPatch, delta: timedelta) -> datetime:
    """Advance every injected clock; authorization ages without sleeping."""
    later = NOW + delta
    monkeypatch.setattr(verification, "utc_now", lambda: later)
    monkeypatch.setattr("apps.identity.physician_registry.utc_now", lambda: later)
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: int(later.timestamp()))
    monkeypatch.setattr(signing, "utc_now", lambda: later)
    monkeypatch.setattr(provider_module, "utc_now", lambda: later)
    return later


def test_delayed_signature_requires_current_authorization(
    setup: SigningSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed callback arriving after the authorization deadline fails."""
    operation = setup.request_signature()
    assert operation.state == "signing"
    later = _advance_clocks(monkeypatch, timedelta(minutes=10))
    headers, body = setup.callback(operation)
    assert setup.receive(headers, body) == "applied"
    operation = setup.refresh(operation)
    assert operation.state == "failed"
    assert operation.failure_reason == "authorization_expired"
    assert operation.signed_bytes is None
    # Recovery requires renewed step-up and a fresh evidence check.
    setup.request.session[STEP_UP_SESSION_KEY] = int(later.timestamp())
    fresh = setup.request_signature()
    assert fresh.state == "signing"
    assert fresh.pk != operation.pk
    assert fresh.authorized_until == later + timedelta(minutes=5)
    headers, body = setup.callback(fresh)
    assert setup.receive(headers, body) == "applied"
    fresh = setup.refresh(fresh)
    assert fresh.state == "rehearsal_complete"


def test_stale_retry_requires_renewed_authorization(
    setup: SigningSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-dispatch after the deadline cannot turn expired authority into issuance."""
    graph = setup.graph
    operation = setup.initiate()
    setup.prepare(operation.pk)
    operation = setup.refresh(operation)
    assert operation.state == "prepared"
    _advance_clocks(monkeypatch, timedelta(minutes=10))
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        retry_dispatch(clinic_id=graph.clinic_a, operation_id=operation.pk)
    operation = setup.refresh(operation)
    assert operation.state == "signing"
    headers, body = setup.callback(operation)
    assert setup.receive(headers, body) == "applied"
    operation = setup.refresh(operation)
    assert operation.state == "failed"
    assert operation.failure_reason == "authorization_expired"


def test_failed_physician_evidence_blocks_and_recovers(
    setup: SigningSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RevokedRegistry(SyntheticPhysicianRegistry):
        def lookup(self, identity: RegistrationIdentity) -> RegistryResponse:
            response = super().lookup(identity)
            return RegistryResponse(
                identity=response.identity,
                status="revoked",
                checked_at=response.checked_at,
                expires_at=response.expires_at,
                recheck_at=response.recheck_at,
                reference=response.reference,
                synthetic=response.synthetic,
            )

    monkeypatch.setattr(verification, "get_physician_registry", RevokedRegistry)
    graph = setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(PhysicianVerificationRequired, match="registration_revoked"):
            request_signature(
                request=setup.request,
                clinic_id=graph.clinic_a,
                document_id=setup.document.pk,
            )
        operation = SignatureOperation.objects.get()
        assert operation.state == "draft"
        assert PhysicianEvidence.objects.get().reason_code == "registration_revoked"
    monkeypatch.setattr(
        verification, "get_physician_registry", SyntheticPhysicianRegistry
    )
    operation = setup.request_signature()
    assert operation.state == "signing"
    assert operation.evidence_id is not None


def test_content_digest_is_bound_to_exact_document_bytes(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    operation = setup.request_signature()
    assert operation.state == "signing"
    headers, body = setup.callback(
        operation, signed_bytes=bytes(setup.document.pdf_bytes) + b"tampered"
    )
    assert setup.receive(headers, body) == "applied"
    operation = setup.refresh(operation)
    assert operation.state == "failed"
    assert operation.failure_reason == "signature_invalid"
    assert operation.signed_bytes is None
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        SignatureOperation.objects.create(
            organization_id=graph.organization_a,
            document=setup.document,
            encounter_id=setup.document.encounter_id,
            clinic_id=graph.clinic_a,
            patient_id=setup.document.patient_id,
            issuer_id=graph.physician,
            provider=SYNTHETIC_PROVIDER,
            content_digest="0" * 64,
            signer_subject=setup.profile.signing_subject,
        )


def test_callback_authentication_and_binding_fail_closed(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    operation = setup.request_signature()
    headers, body = setup.callback(operation)
    payload = json.loads(body)
    forged_body = json.dumps({**payload, "event_id": "event-forged"}).encode()
    with pytest.raises(SignatureCallbackError):
        setup.receive({SIGNATURE_HEADER: "0" * 64}, forged_body)
    with pytest.raises(SignatureCallbackError):
        setup.receive(headers, b"not-json")
    with pytest.raises(SignatureCallbackError):
        setup.receive(headers, body, provider="unknown-provider")
    unknown = {**payload, "operation_id": "synop-unknown", "event_id": "e2"}
    unknown_body = json.dumps(unknown).encode()
    unknown_headers = {
        SIGNATURE_HEADER: hmac.new(
            provider_module.SYNTHETIC_SECRET, unknown_body, hashlib.sha256
        ).hexdigest()
    }
    assert setup.receive(unknown_headers, unknown_body) == "rejected"
    operation = setup.refresh(operation)
    assert operation.state == "signing"
    assert setup.receive(headers, body) == "applied"
    operation = setup.refresh(operation)
    assert operation.state == "rehearsal_complete"
    assert setup.receive(headers, body) == "duplicate"
    late = {**payload, "event_id": "event-late"}
    late_body = json.dumps(late).encode()
    late_headers = {
        SIGNATURE_HEADER: hmac.new(
            provider_module.SYNTHETIC_SECRET, late_body, hashlib.sha256
        ).hexdigest()
    }
    assert setup.receive(late_headers, late_body) == "rejected"
    with owner_scope(graph.organization_a):
        assert SignatureCallback.objects.filter(operation_id=operation.pk).count() == 1


def test_wrong_signer_and_failed_report_never_issue(setup: SigningSetup) -> None:
    operation = setup.request_signature()
    request = SignatureRequest(
        operation_id=operation.pk,
        issuer_id=operation.issuer_id,
        signer_subject="synthetic:physician:other",
        content_digest=operation.content_digest,
        content=bytes(setup.document.pdf_bytes),
    )
    headers, body = setup.provider.sign(request, operation.operation_id)
    assert setup.receive(headers, body) == "applied"
    operation = setup.refresh(operation)
    assert operation.state == "failed"
    assert operation.failure_reason == "signature_invalid"

    second = setup.request_signature()
    headers, body = setup.callback(second, status="failed")
    assert setup.receive(headers, body) == "applied"
    second = setup.refresh(second)
    assert second.state == "failed"
    assert second.failure_reason == "provider_reported"
    assert second.signed_bytes is None


def test_provider_timeout_rejection_and_unavailable_fail_closed(
    setup: SigningSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = setup.initiate()
    setup.prepare(operation.pk)
    operation = setup.refresh(operation)
    assert operation.state == "prepared"

    class TimeoutProvider(SyntheticSignatureProvider):
        def begin(self, request: SignatureRequest) -> SignatureAcceptance:
            raise SignatureProviderTransientError

    monkeypatch.setattr(
        signing, "signature_provider_for", lambda name: TimeoutProvider()
    )
    assert setup.dispatch(operation.pk) == "retry"
    operation = setup.refresh(operation)
    assert operation.state == "prepared"
    assert operation.operation_id == ""

    class RejectingProvider(SyntheticSignatureProvider):
        def begin(self, request: SignatureRequest) -> SignatureAcceptance:
            raise SignatureProviderRejectedError

    monkeypatch.setattr(
        signing, "signature_provider_for", lambda name: RejectingProvider()
    )
    assert setup.dispatch(operation.pk) == "failed"
    operation = setup.refresh(operation)
    assert operation.state == "failed"
    assert operation.failure_reason == "provider_rejected"

    monkeypatch.setattr(signing, "signature_provider_for", _provider_for)
    recovered = setup.request_signature()
    assert recovered.state == "signing"

    monkeypatch.setattr(signing, "signature_provider_for", _unavailable_provider)
    stalled = setup.initiate()
    setup.prepare(stalled.pk)
    assert setup.dispatch(stalled.pk) == "failed"
    stalled = setup.refresh(stalled)
    assert stalled.failure_reason == "provider_unavailable"


def test_completed_output_is_immutable_and_original_bytes_survive_amendment(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    operation = setup.issue()
    original_pdf = bytes(setup.document.pdf_bytes)
    signed = bytes(operation.signed_bytes)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        save_draft(
            **setup.scope,
            draft_id=setup.document.draft_id,
            category="synthetic_non_controlled",
            expected_version=setup.document.document_version,
            items=[{**ITEM, "dose": "Dose alterada"}],
        )
        amended = PrescriptionDocument.objects.get(pk=setup.document.pk)
        assert bytes(amended.pdf_bytes) == original_pdf
        stored = SignatureOperation.objects.get(pk=operation.pk)
        assert bytes(stored.signed_bytes) == signed
        assert stored.signed_digest == sha256(signed).hexdigest()
        assert stored.state == "rehearsal_complete"
        with pytest.raises(DatabaseError), transaction.atomic():
            SignatureOperation.objects.filter(pk=operation.pk).update(
                signed_bytes=b"forged"
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            SignatureCallback.objects.filter(operation_id=operation.pk).update(
                payload={}
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            SignatureCallback.objects.filter(operation_id=operation.pk).delete()
        with pytest.raises(ClinicalConflictError, match="already_completed"):
            initiate_signature(clinic_id=graph.clinic_a, document_id=setup.document.pk)


def test_abandon_recovers_live_operation(setup: SigningSetup) -> None:
    graph = setup.graph
    operation = setup.request_signature()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        abandoned = abandon_signature(
            clinic_id=graph.clinic_a, operation_id=operation.pk
        )
        assert abandoned.state == "failed"
        assert abandoned.failure_reason == "abandoned"
        assert abandoned.completed_at == NOW
        with pytest.raises(ClinicalConflictError):
            abandon_signature(clinic_id=graph.clinic_a, operation_id=operation.pk)
    fresh = setup.request_signature()
    assert fresh.state == "signing"
    assert fresh.pk != operation.pk


def test_non_issuer_and_cross_tenant_cannot_reach_operations(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    operation = setup.request_signature()
    with owner_scope(graph.organization_a):
        UserClinicRole.objects.create(
            user_id=graph.shared_user,
            clinic_id=graph.clinic_a,
            organization_id=graph.organization_a,
            role="physician",
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        assert not SignatureOperation.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            signature_status(clinic_id=graph.clinic_a, operation_id=operation.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            download_signed_document(
                clinic_id=graph.clinic_a, operation_id=operation.pk
            )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert not SignatureOperation.objects.exists()
    with runtime_role():
        assert not SignatureOperation.objects.exists()
        assert not SignatureCallback.objects.exists()


def test_capability_disabled_blocks_initiation(setup: SigningSetup) -> None:
    graph = setup.graph
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        override_settings(PRESCRIPTION_SYNTHETIC_SIGNING=False),
    ):
        with pytest.raises(SignatureUnavailableError):
            initiate_signature(clinic_id=graph.clinic_a, document_id=setup.document.pk)
        assert not SignatureOperation.objects.exists()


def test_missing_profile_blocks_before_any_operation(setup: SigningSetup) -> None:
    graph = setup.graph
    with owner_scope(graph.organization_a):
        PhysicianProfile.objects.filter(pk=setup.profile.pk).delete()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        with pytest.raises(
            PhysicianVerificationRequired, match="registration_profile_required"
        ):
            initiate_signature(clinic_id=graph.clinic_a, document_id=setup.document.pk)
        assert not SignatureOperation.objects.exists()


def test_exact_force_rls_policies(setup: SigningSetup) -> None:
    tables = [
        "prescription_signatureoperation",
        "prescription_signaturecallback",
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relname = ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {(table, True, True) for table in tables}
        cursor.execute(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename = ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            ("prescription_signatureoperation", "setup_tenant"),
            ("prescription_signatureoperation", "signature_read"),
            ("prescription_signatureoperation", "signature_insert"),
            ("prescription_signatureoperation", "signature_update"),
            ("prescription_signaturecallback", "setup_tenant"),
            ("prescription_signaturecallback", "signature_callback_read"),
            ("prescription_signaturecallback", "signature_callback_insert"),
        }


def test_http_signing_flow_and_authenticated_callback(
    rbac_graph: RbacGraph,
) -> None:
    scope, document = seed_rendered(rbac_graph)
    with owner_scope(rbac_graph.organization_a):
        PhysicianProfile.objects.create(
            organization_id=rbac_graph.organization_a,
            user_id=rbac_graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-34",
            signing_subject=f"synthetic:physician:{rbac_graph.physician}",
        )
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    callback_url = f"/prescription/signing/callback/{SYNTHETIC_PROVIDER}/"
    provider = SyntheticSignatureProvider()
    with override_settings(
        PHYSICIAN_SYNTHETIC_REGISTRY=True, PRESCRIPTION_SYNTHETIC_SIGNING=True
    ):
        with physician_client(rbac_graph) as client:
            assert (
                client.post(
                    url, {"action": "open", "encounter_id": scope["encounter_id"]}
                ).status_code
                == 302
            )
            response = client.post(
                url,
                {
                    "action": "sign_document",
                    "encounter_id": scope["encounter_id"],
                    "document_id": str(document.pk),
                },
            )
            assert response.status_code == 302
            status_url = response.headers["Location"]
            page = client.get(status_url)
            assert page.status_code == 200
            assert b'data-state="signing"' in page.content
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            operation = SignatureOperation.objects.get()
            assert operation.state == "signing"
            request = SignatureRequest(
                operation_id=operation.pk,
                issuer_id=operation.issuer_id,
                signer_subject=operation.signer_subject,
                content_digest=operation.content_digest,
                content=bytes(document.pdf_bytes),
            )
            headers, body = provider.sign(request, operation.operation_id)
        with runtime_role():
            forged = physician_anonymous_post(callback_url, body, "0" * 64)
            assert forged.status_code == 403
            applied = physician_anonymous_post(
                callback_url, body, headers[SIGNATURE_HEADER]
            )
            assert applied.status_code == 200
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            operation.refresh_from_db()
            assert operation.state == "rehearsal_complete"
            signed = bytes(operation.signed_bytes)
        with physician_client(rbac_graph) as client:
            assert (
                client.post(
                    url, {"action": "open", "encounter_id": scope["encounter_id"]}
                ).status_code
                == 302
            )
            page = client.get(status_url)
            assert page.status_code == 200
            assert b'data-state="rehearsal_complete"' in page.content
            download = client.post(status_url, {"action": "download_signed"})
            assert download.status_code == 200
            assert download.content == signed
            assert "no-store" in download.headers["Cache-Control"]


class _ActionControls(HTMLParser):
    """Collect rendered form actions and links from a status/draft page."""

    def __init__(self) -> None:
        super().__init__()
        self.actions: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "button" and fields.get("name") == "action":
            self.actions.append(str(fields.get("value")))
        if tag == "a" and fields.get("href"):
            self.links.append(str(fields["href"]))


def test_http_failed_operation_recovers_through_rendered_controls(
    rbac_graph: RbacGraph,
) -> None:
    """A provider-reported failure exposes an authorized fresh attempt."""
    scope, document = seed_rendered(rbac_graph)
    with owner_scope(rbac_graph.organization_a):
        PhysicianProfile.objects.create(
            organization_id=rbac_graph.organization_a,
            user_id=rbac_graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-34",
            signing_subject=f"synthetic:physician:{rbac_graph.physician}",
        )
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    callback_url = f"/prescription/signing/callback/{SYNTHETIC_PROVIDER}/"
    with override_settings(
        PHYSICIAN_SYNTHETIC_REGISTRY=True, PRESCRIPTION_SYNTHETIC_SIGNING=True
    ):
        with physician_client(rbac_graph) as client:
            assert (
                client.post(
                    url, {"action": "open", "encounter_id": scope["encounter_id"]}
                ).status_code
                == 302
            )
            response = client.post(
                url,
                {
                    "action": "sign_document",
                    "encounter_id": scope["encounter_id"],
                    "document_id": str(document.pk),
                },
            )
            assert response.status_code == 302
            status_url = response.headers["Location"]
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            operation = SignatureOperation.objects.get()
            assert operation.state == "signing"
            payload = {
                "event_id": "event-failed",
                "operation_id": operation.operation_id,
                "status": "failed",
            }
            body = json.dumps(payload).encode()
            signature = hmac.new(
                provider_module.SYNTHETIC_SECRET, body, hashlib.sha256
            ).hexdigest()
        with runtime_role():
            applied = physician_anonymous_post(callback_url, body, signature)
            assert applied.status_code == 200
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            operation.refresh_from_db()
            assert operation.state == "failed"
            assert operation.failure_reason == "provider_reported"
        with physician_client(rbac_graph) as client:
            assert (
                client.post(
                    url, {"action": "open", "encounter_id": scope["encounter_id"]}
                ).status_code
                == 302
            )
            draft_page = client.get(url)
            status_page = client.get(status_url)
            assert draft_page.status_code == status_page.status_code == 200
            draft_controls, status_controls = _ActionControls(), _ActionControls()
            draft_controls.feed(draft_page.content.decode())
            status_controls.feed(status_page.content.decode())
            # The draft offers a fresh attempt on the same immutable document
            # (through its final review screen) and the failed status page
            # offers restart directly.
            assert "review_document" in draft_controls.actions
            assert "restart_signature" in status_controls.actions
            restarted = client.post(status_url, {"action": "restart_signature"})
            assert restarted.status_code == 302
            assert restarted.headers["Location"] != status_url
            fresh_page = client.get(restarted.headers["Location"])
            assert fresh_page.status_code == 200
            assert b'data-state="signing"' in fresh_page.content
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            fresh = SignatureOperation.objects.exclude(pk=operation.pk).get()
            assert fresh.state == "signing"
            assert fresh.document_id == document.pk
            assert fresh.content_digest == document.pdf_digest


def physician_anonymous_post(url: str, body: bytes, signature: str) -> HttpResponseBase:
    """POST one provider callback with no session, as the real provider would."""
    from django.test import Client  # noqa: PLC0415

    return Client().post(
        url,
        body,
        content_type="application/json",
        HTTP_X_SYNTHETIC_SIGNATURE=signature,
    )


def test_synthetic_provider_rejects_unlabelled_signer() -> None:
    provider = SyntheticSignatureProvider()
    request = SignatureRequest(
        operation_id=uuid4(),
        issuer_id=uuid4(),
        signer_subject="unapproved",
        content_digest="0" * 64,
        content=b"content",
    )
    with override_settings(PRESCRIPTION_SYNTHETIC_SIGNING=True):
        with pytest.raises(SignatureProviderRejectedError):
            provider.begin(request)
        with pytest.raises(SignatureVerificationError):
            provider.verify_signature(
                signed_bytes=b"forged",
                content_digest="0" * 64,
                signer_subject="synthetic:x",
                operation_ref="synop-x",
                not_before=NOW - timedelta(minutes=1),
            )
    with (
        override_settings(PRESCRIPTION_SYNTHETIC_SIGNING=False),
        pytest.raises(SignatureProviderUnavailableError),
    ):
        provider.begin(request)
