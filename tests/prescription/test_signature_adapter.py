"""Todo 35: independently verified recorded signing; never live issuance."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.prescription import signature_provider as provider_module
from apps.prescription import signing
from apps.prescription.models import SignatureCallback, SignatureOperation
from apps.prescription.services import render_document, save_draft
from apps.prescription.signature_adapter import (
    RECORDED_PROVIDER,
    RecordedSignatureProvider,
)
from apps.prescription.signature_provider import (
    SIGNATURE_HEADER,
    SYNTHETIC_END,
    SYNTHETIC_MARKER,
    SignatureCallbackError,
    SignatureProviderRejectedError,
    SignatureProviderTransientError,
    SignatureProviderUnavailableError,
    SignatureRequest,
    SignatureVerificationError,
    SyntheticSignatureProvider,
    require_live_signing_capabilities,
    signature_approval_digest,
)
from apps.prescription.signature_verification import (
    RecordedVerificationAPI,
    SignatureVerificationAPIAdapter,
    VerificationRequest,
)
from apps.prescription.verification import release_document
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from django.test import override_settings

from patient_service_support import runtime_role
from provider_gate_support import seed_capabilities
from renewal.test_document_artifacts import ITEM
from renewal.test_signatures import NOW, SigningSetup, owner_scope
from renewal.test_signatures import setup as signing_setup

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ("signing_setup",)
pytestmark = pytest.mark.django_db(transaction=True)

FIXTURE = Path(__file__).parent / "fixtures" / "signature-verification.json"
SIGNED = b"%PDF-1.4\nSynthetic verification fixture; not a qualified signature.\n"
EXPECTATION = VerificationRequest(
    signed_bytes=SIGNED,
    content_digest=sha256(b"Synthetic rendered bytes").hexdigest(),
    signer_subject="synthetic:recorded-signer",
    operation_ref="synthetic-recorded-operation",
    category="synthetic_non_controlled",
    approval_digest="a" * 64,
    not_before=NOW,
)


@pytest.fixture(autouse=True)
def synthetic_verification(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("apps.prescription.signature_verification.utc_now", lambda: NOW)
    with override_settings(PRESCRIPTION_SYNTHETIC_SIGNING=True):
        yield


def recorded_api(case: str, request: VerificationRequest) -> RecordedVerificationAPI:
    fixture = json.loads(FIXTURE.read_text())
    facts = fixture["cases"][case]
    if facts.get("outcome") == "timeout":
        return RecordedVerificationAPI(
            response=None, signed_digest=sha256(request.signed_bytes).hexdigest()
        )
    response = {
        **{key: value for key, value in fixture.items() if key != "cases"},
        **facts,
        "signed_digest": sha256(request.signed_bytes).hexdigest(),
        "content_digest": request.content_digest,
        "signer_subject": request.signer_subject,
        "operation_ref": request.operation_ref,
        "category": request.category,
        "approval_digest": request.approval_digest,
        "signed_at": NOW.isoformat(),
        "checked_at": NOW.isoformat(),
    }
    return RecordedVerificationAPI(
        response=json.dumps(response).encode(),
        signed_digest=sha256(request.signed_bytes).hexdigest(),
    )


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("valid", None),
        ("invalid_chain", SignatureVerificationError),
        ("revoked_cert", SignatureVerificationError),
        ("invalid_timestamp", SignatureVerificationError),
        ("timeout", SignatureProviderTransientError),
    ],
)
def test_recorded_independent_verification(
    case: str,
    error: type[Exception] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.prescription.signature_verification.utc_now", lambda: NOW)
    api = recorded_api(case, EXPECTATION)
    verifier = SignatureVerificationAPIAdapter(api)
    assert not connection.in_atomic_block
    if error is not None:
        with pytest.raises(error):
            verifier.verify(EXPECTATION)
    else:
        assert verifier.verify(EXPECTATION).signed_at == NOW


@dataclass
class RecordedSetup:
    base: SigningSetup
    provider: RecordedSignatureProvider

    def candidate(
        self, case: str
    ) -> tuple[SignatureOperation, dict[str, str], bytes, VerificationRequest]:
        operation = self.base.request_signature()
        graph = self.base.graph
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            request = signing._operation_request(operation)
        headers, body = self.provider.sign(request, operation.operation_id)
        facts = self.provider.authenticate_callback(headers=headers, body=body)
        assert request.category is not None
        expected = VerificationRequest(
            signed_bytes=facts.signed_bytes,
            content_digest=request.content_digest,
            signer_subject=request.signer_subject,
            operation_ref=operation.operation_id,
            category=request.category,
            approval_digest=request.approval_digest,
            not_before=operation.created_at,
        )
        self.provider.verifier = SignatureVerificationAPIAdapter(
            recorded_api(case, expected)
        )
        return operation, headers, body, expected

    def receive(self, headers: dict[str, str], body: bytes) -> str:
        return self.base.receive(headers, body, provider=RECORDED_PROVIDER)

    def reconcile(self, operation: SignatureOperation) -> str:
        with runtime_role():
            return signing.reconcile_signature(operation.pk)


@pytest.fixture
def recorded(
    signing_setup: SigningSetup, monkeypatch: pytest.MonkeyPatch
) -> RecordedSetup:
    adapter = RecordedSignatureProvider(
        SignatureVerificationAPIAdapter(recorded_api("timeout", EXPECTATION))
    )
    monkeypatch.setattr(signing, "get_signature_provider", lambda: adapter)
    monkeypatch.setattr(signing, "signature_provider_for", lambda name: adapter)
    return RecordedSetup(signing_setup, adapter)


@pytest.mark.django_db(transaction=True)
def test_candidate_is_committed_before_independent_verification_and_never_issued(
    recorded: RecordedSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation, headers, body, expected = recorded.candidate("valid")
    seen: list[str] = []
    api = recorded_api("valid", expected)

    class ObservedAPI:
        def verify(self, signed_bytes: bytes) -> bytes:
            assert not connection.in_atomic_block
            with tenant_context(
                recorded.base.graph.physician, recorded.base.graph.organization_a
            ):
                candidate = SignatureOperation.objects.get(pk=operation.pk)
                seen.append(candidate.state)
                assert candidate.completed_at is None
                assert bytes(candidate.signed_bytes) == signed_bytes
            return api.verify(signed_bytes)

    def signer_verification_is_not_independent(
        self: SyntheticSignatureProvider, **kwargs: object
    ) -> None:
        pytest.fail("The signing provider must not verify its own completion")

    monkeypatch.setattr(
        SyntheticSignatureProvider,
        "verify_signature",
        signer_verification_is_not_independent,
    )
    recorded.provider.verifier = SignatureVerificationAPIAdapter(ObservedAPI())
    assert recorded.receive(headers, body) == "applied"
    final = recorded.base.refresh(operation)
    assert seen == ["signed_unverified"]
    assert final.state == "rehearsal_complete"
    assert final.completed_at == NOW
    assert bytes(final.signed_bytes) == expected.signed_bytes
    assert final.content_digest == recorded.base.document.pdf_digest
    assert final.evidence_snapshot is not None
    assert final.evidence_snapshot["approval_digest"] == expected.approval_digest
    assert recorded.provider.status(operation.operation_id) is not None
    with owner_scope(recorded.base.graph.organization_a):
        assert not SignatureOperation.objects.filter(state="issued").exists()
        assert SignatureCallback.objects.filter(operation=operation).count() == 1
        with pytest.raises(DatabaseError), transaction.atomic():
            SignatureOperation.objects.filter(pk=operation.pk).update(state="issued")
    assert recorded.receive(headers, body) == "duplicate"
    assert recorded.reconcile(operation) == "skipped"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("case", ["invalid_chain", "revoked_cert", "invalid_timestamp"])
def test_negative_verification_quarantines_candidate_without_release(
    recorded: RecordedSetup, case: str
) -> None:
    operation, headers, body, expected = recorded.candidate(case)
    assert recorded.receive(headers, body) == "applied"
    final = recorded.base.refresh(operation)
    assert final.state == "failed"
    assert final.failure_reason == "signature_invalid"
    assert bytes(final.signed_bytes) == expected.signed_bytes
    graph = recorded.base.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(ClinicalAccessDeniedError):
            signing.download_signed_document(
                clinic_id=graph.clinic_a, operation_id=operation.pk
            )
        with pytest.raises(ClinicalConflictError, match="not_issued"):
            release_document(clinic_id=graph.clinic_a, document_id=final.document_id)


@pytest.mark.django_db(transaction=True)
def test_timeout_reconciles_the_same_bytes_after_worker_restart_without_resigning(
    recorded: RecordedSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation, headers, body, expected = recorded.candidate("timeout")
    assert recorded.receive(headers, body) == "reconcile"
    pending = recorded.base.refresh(operation)
    assert pending.state == "signed_unverified"
    assert pending.completed_at is None
    assert pending.failure_reason == ""
    assert bytes(pending.signed_bytes) == expected.signed_bytes
    assert recorded.receive(headers, body) == "duplicate"
    assert recorded.base.dispatch(operation.pk) == "reconcile"
    restarted = RecordedSignatureProvider(
        SignatureVerificationAPIAdapter(recorded_api("valid", expected))
    )
    monkeypatch.setattr(signing, "signature_provider_for", lambda name: restarted)
    assert recorded.reconcile(operation) == "completed"
    final = recorded.base.refresh(operation)
    assert final.state == "rehearsal_complete"
    assert final.operation_id == expected.operation_ref
    assert bytes(final.signed_bytes) == expected.signed_bytes
    with owner_scope(recorded.base.graph.organization_a):
        assert SignatureOperation.objects.count() == 1
        assert SignatureCallback.objects.count() == 1


def authenticated_body(payload: dict[str, object]) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    return {
        SIGNATURE_HEADER: hmac.new(
            provider_module.SYNTHETIC_SECRET, body, hashlib.sha256
        ).hexdigest()
    }, body


@pytest.mark.django_db(transaction=True)
def test_independent_verifier_rejects_tampered_signed_bytes(
    recorded: RecordedSetup,
) -> None:
    operation, _, body, expected = recorded.candidate("valid")
    payload = json.loads(body)
    payload["signed_bytes"] = base64.b64encode(
        b"tampered-" + expected.signed_bytes
    ).decode("ascii")
    headers, altered = authenticated_body(payload)
    assert recorded.receive(headers, altered) == "applied"
    final = recorded.base.refresh(operation)
    assert final.state == "failed"
    assert final.failure_reason == "signature_invalid"


@pytest.mark.django_db(transaction=True)
def test_callback_replayed_for_another_document_is_rejected_before_scope_lookup(
    recorded: RecordedSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation, _, body, _ = recorded.candidate("valid")
    payload = json.loads(body)
    payload["operation_id"] = "synthetic-another-document-operation"
    headers, altered = authenticated_body(payload)

    def scope_must_not_be_read(provider: str, operation_ref: str) -> None:
        pytest.fail("A cross-document replay reached scope resolution")

    monkeypatch.setattr(signing, "_resolve_callback_scope", scope_must_not_be_read)
    with pytest.raises(SignatureCallbackError):
        recorded.receive(headers, altered)
    assert recorded.base.refresh(operation).state == "signing"
    with owner_scope(recorded.base.graph.organization_a):
        assert SignatureCallback.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_pending_candidate_is_immutable_and_cannot_be_released(
    recorded: RecordedSetup,
) -> None:
    operation, headers, body, _ = recorded.candidate("timeout")
    assert recorded.receive(headers, body) == "reconcile"
    graph = recorded.base.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        changes_list: list[dict[str, object]] = [
            {"signed_bytes": b"forged"},
            {"signed_digest": "b" * 64},
            {"evidence_snapshot": {}},
            {"state": "issued", "completed_at": NOW},
        ]
        for changes in changes_list:
            with pytest.raises(DatabaseError), transaction.atomic():
                SignatureOperation.objects.filter(pk=operation.pk).update(**changes)
        with pytest.raises(ClinicalAccessDeniedError):
            signing.download_signed_document(
                clinic_id=graph.clinic_a, operation_id=operation.pk
            )
        with pytest.raises(ClinicalConflictError, match="not_issued"):
            release_document(
                clinic_id=graph.clinic_a, document_id=recorded.base.document.pk
            )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["edit", "expiry"])
def test_approval_is_revalidated_after_verification_timeout(
    recorded: RecordedSetup, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    operation, headers, body, expected = recorded.candidate("timeout")
    assert recorded.receive(headers, body) == "reconcile"
    graph = recorded.base.graph
    if change == "edit":
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            save_draft(
                **recorded.base.scope,
                draft_id=recorded.base.document.draft_id,
                category="synthetic_non_controlled",
                expected_version=recorded.base.document.document_version,
                items=[{**ITEM, "dose": "Synthetic amended dose"}],
            )
    else:
        pending = recorded.base.refresh(operation)
        assert pending.authorized_until is not None
        monkeypatch.setattr(signing, "utc_now", lambda: pending.authorized_until)
    recorded.provider.verifier = SignatureVerificationAPIAdapter(
        recorded_api("valid", expected)
    )
    assert recorded.reconcile(operation) == "failed"
    final = recorded.base.refresh(operation)
    assert final.state == "failed"
    assert final.failure_reason == (
        "stale_document_approval" if change == "edit" else "authorization_expired"
    )


def test_stale_initiation_does_not_supersede_a_pending_candidate(
    recorded: RecordedSetup,
) -> None:
    operation, headers, body, _ = recorded.candidate("timeout")
    assert recorded.receive(headers, body) == "reconcile"
    graph = recorded.base.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        save_draft(
            **recorded.base.scope,
            draft_id=recorded.base.document.draft_id,
            category="synthetic_non_controlled",
            expected_version=recorded.base.document.document_version,
            items=[{**ITEM, "dose": "Synthetic amended dose"}],
        )
    with owner_scope(graph.organization_a):
        events = AuditEvent.objects.count()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        before = SignatureOperation.objects.get(pk=operation.pk)
        callbacks = SignatureCallback.objects.count()
        with pytest.raises(ClinicalConflictError, match="stale_document_approval"):
            signing.initiate_signature(
                clinic_id=graph.clinic_a, document_id=recorded.base.document.pk
            )
        after = SignatureOperation.objects.get(pk=operation.pk)
        assert after.state == before.state == "signed_unverified"
        assert after.failure_reason == before.failure_reason == ""
        assert after.completed_at is before.completed_at is None
        assert SignatureOperation.objects.count() == 1
        assert SignatureCallback.objects.count() == callbacks
    with owner_scope(graph.organization_a):
        assert AuditEvent.objects.count() == events


@pytest.mark.django_db(transaction=True)
def test_unactivated_signing_and_verification_capabilities_refuse_without_side_effects(
    recorded: RecordedSetup,
) -> None:
    seed_capabilities()
    graph = recorded.base.graph
    with owner_scope(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.audit_event), "
            "(SELECT count(*) FROM clinic_app.comms_integrationoperation), "
            "(SELECT count(*) FROM clinic_app.prescription_signatureoperation)"
        )
        before = cursor.fetchone()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(SignatureProviderUnavailableError),
    ):
        require_live_signing_capabilities(clinic_id=graph.clinic_a)
    with owner_scope(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.audit_event), "
            "(SELECT count(*) FROM clinic_app.comms_integrationoperation), "
            "(SELECT count(*) FROM clinic_app.prescription_signatureoperation)"
        )
        assert cursor.fetchone() == before


@pytest.mark.parametrize(
    "field", ["content", "signer_subject", "category", "document_version"]
)
def test_preparation_refuses_changed_approval(field: str) -> None:
    request = SignatureRequest(
        operation_id=uuid4(),
        issuer_id=uuid4(),
        signer_subject="synthetic:recorded-signer",
        content_digest=sha256(b"Synthetic bytes").hexdigest(),
        content=b"Synthetic bytes",
        category="synthetic_non_controlled",
        document_version=1,
    )
    request = replace(request, approval_digest=signature_approval_digest(request))
    changed = {
        "content": replace(request, content=b"Changed bytes"),
        "signer_subject": replace(request, signer_subject="synthetic:other-signer"),
        "category": replace(request, category="simple"),
        "document_version": replace(request, document_version=2),
    }[field]
    adapter = RecordedSignatureProvider(
        SignatureVerificationAPIAdapter(recorded_api("valid", EXPECTATION))
    )
    with pytest.raises(SignatureProviderRejectedError):
        adapter.prepare(changed)


@pytest.mark.parametrize(
    "field",
    [
        "signed_digest",
        "content_digest",
        "signer_subject",
        "operation_ref",
        "category",
        "approval_digest",
        "policy",
        "provenance",
        "format",
        "schema_version",
        "signed_at",
        "checked_at",
    ],
)
def test_independent_response_must_match_every_binding(field: str) -> None:
    api = recorded_api("valid", EXPECTATION)
    assert api.response is not None
    payload = json.loads(api.response)
    payload[field] = "synthetic-mismatched-value"
    verifier = SignatureVerificationAPIAdapter(
        RecordedVerificationAPI(
            response=json.dumps(payload).encode(), signed_digest=api.signed_digest
        )
    )
    with pytest.raises(SignatureVerificationError):
        verifier.verify(EXPECTATION)


@pytest.mark.parametrize(
    "shape", ["missing", "unknown", "duplicate", "malformed", "large"]
)
def test_independent_response_is_closed_and_bounded(shape: str) -> None:
    api = recorded_api("valid", EXPECTATION)
    assert api.response is not None
    payload = json.loads(api.response)
    if shape == "missing":
        del payload["revocation"]
    if shape == "unknown":
        payload["unexpected"] = True
    response = json.dumps(payload).encode()
    if shape == "duplicate":
        response = response[:-1] + b',"chain":"trusted"}'
    if shape == "malformed":
        response = b"not-json"
    if shape == "large":
        response = b"x" * 8193
    verifier = SignatureVerificationAPIAdapter(
        RecordedVerificationAPI(response=response, signed_digest=api.signed_digest)
    )
    with pytest.raises(SignatureVerificationError):
        verifier.verify(EXPECTATION)


@pytest.mark.django_db(transaction=True)
def test_only_one_worker_can_verify_a_pending_candidate(
    recorded: RecordedSetup,
) -> None:
    operation, headers, body, expected = recorded.candidate("timeout")
    assert recorded.receive(headers, body) == "reconcile"
    entered = Event()
    finish = Event()
    api = recorded_api("valid", expected)

    class PausedAPI:
        def verify(self, signed_bytes: bytes) -> bytes:
            entered.set()
            assert finish.wait(timeout=15)
            return api.verify(signed_bytes)

    def worker() -> str:
        try:
            return recorded.reconcile(operation)
        finally:
            connections.close_all()

    recorded.provider.verifier = SignatureVerificationAPIAdapter(PausedAPI())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert entered.wait(timeout=15)
            assert recorded.reconcile(operation) == "reconcile"
        finally:
            finish.set()
        assert future.result(timeout=15) == "completed"
    assert recorded.base.refresh(operation).state == "rehearsal_complete"


@pytest.mark.django_db(transaction=True)
def test_expiry_during_independent_verification_is_rechecked_before_completion(
    recorded: RecordedSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation, headers, body, expected = recorded.candidate("valid")
    assert operation.authorized_until is not None
    deadline = operation.authorized_until
    api = recorded_api("valid", expected)

    class ExpiringAPI:
        def verify(self, signed_bytes: bytes) -> bytes:
            monkeypatch.setattr(signing, "utc_now", lambda: deadline)
            return api.verify(signed_bytes)

    recorded.provider.verifier = SignatureVerificationAPIAdapter(ExpiringAPI())
    assert recorded.receive(headers, body) == "applied"
    final = recorded.base.refresh(operation)
    assert final.state == "failed"
    assert final.failure_reason == "authorization_expired"


@pytest.mark.django_db(transaction=True)
def test_clinical_sentinel_stays_out_of_audit_outbox_and_logs(
    recorded: RecordedSetup, caplog: pytest.LogCaptureFixture
) -> None:
    graph = recorded.base.graph
    sentinel = "SINTETICO-SENTINELA-35"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        draft = save_draft(
            **recorded.base.scope,
            draft_id=recorded.base.document.draft_id,
            category="synthetic_non_controlled",
            expected_version=recorded.base.document.document_version,
            items=[{**ITEM, "instructions": sentinel}],
        )
        recorded.base.document = render_document(
            **recorded.base.scope, draft_id=draft.pk, expected_version=draft.version
        )
    operation, headers, body, _ = recorded.candidate("valid")
    assert recorded.receive(headers, body) == "applied"
    assert recorded.base.refresh(operation).state == "rehearsal_complete"
    with owner_scope(graph.organization_a), connection.cursor() as cursor:
        payloads = list(AuditEvent.objects.values_list("payload", flat=True))
        assert sentinel not in json.dumps(payloads)
        assert all(
            set(payload)
            <= {
                "clinic_id",
                "http_method",
                "http_status",
                "object_verb",
                "reason_code",
                "request_id",
            }
            for payload in payloads
        )
        cursor.execute("SELECT count(*) FROM clinic_app.comms_integrationoperation")
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT position(%s::bytea in signed_bytes) "
            "FROM clinic_app.prescription_signatureoperation WHERE id=%s",
            [sentinel.encode(), operation.pk],
        )
        assert cursor.fetchone() == (0,)
    assert sentinel not in caplog.text


def test_recorded_adapter_keeps_the_old_verification_contract_independent(
    recorded: RecordedSetup,
) -> None:
    operation, _, _, expected = recorded.candidate("valid")
    assert (
        recorded.provider.verify_signature(
            signed_bytes=expected.signed_bytes,
            content_digest=expected.content_digest,
            signer_subject=expected.signer_subject,
            operation_ref=expected.operation_ref,
            not_before=expected.not_before,
        ).signed_at
        == NOW
    )
    with pytest.raises(SignatureVerificationError):
        recorded.provider.verify_signature(
            signed_bytes=expected.signed_bytes,
            content_digest=expected.content_digest,
            signer_subject=expected.signer_subject,
            operation_ref="synthetic-unknown-operation",
            not_before=expected.not_before,
        )
    graph = recorded.base.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        request = signing._operation_request(operation)
    with pytest.raises(SignatureProviderRejectedError):
        recorded.provider.sign(
            replace(request, category="simple"), operation.operation_id
        )


@pytest.mark.parametrize(
    "signed_bytes",
    [
        b"Synthetic malformed envelope",
        b"Synthetic content" + SYNTHETIC_MARKER + b"{not-json" + SYNTHETIC_END,
    ],
)
def test_recorded_callback_refuses_malformed_envelopes_before_lookup(
    recorded: RecordedSetup, signed_bytes: bytes
) -> None:
    _, _, body, _ = recorded.candidate("valid")
    payload = json.loads(body)
    payload["signed_bytes"] = base64.b64encode(signed_bytes).decode("ascii")
    headers, body = authenticated_body(payload)
    with pytest.raises(SignatureCallbackError):
        recorded.provider.authenticate_callback(headers=headers, body=body)


def test_independent_api_is_never_called_inside_a_transaction() -> None:
    calls: list[bytes] = []

    class ObservedAPI:
        def verify(self, signed_bytes: bytes) -> bytes:
            calls.append(signed_bytes)
            return b"unreachable"

    with transaction.atomic(), pytest.raises(SignatureProviderUnavailableError):
        SignatureVerificationAPIAdapter(ObservedAPI()).verify(EXPECTATION)
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signed_at", False),
        ("signed_at", NOW.replace(tzinfo=None).isoformat()),
        ("checked_at", "2000-01-01T00:00:00+00:00"),
    ],
)
def test_independent_timestamp_evidence_is_aware_typed_and_ordered(
    field: str, value: object
) -> None:
    api = recorded_api("valid", EXPECTATION)
    assert api.response is not None
    report = json.loads(api.response)
    report[field] = value
    adapter = SignatureVerificationAPIAdapter(
        RecordedVerificationAPI(
            response=json.dumps(report).encode(), signed_digest=api.signed_digest
        )
    )
    with pytest.raises(SignatureVerificationError):
        adapter.verify(EXPECTATION)


@pytest.mark.parametrize("provider", ["legacy", "unavailable"])
def test_missing_independent_verifier_cannot_complete_a_candidate(
    recorded: RecordedSetup, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    operation, headers, body, _ = recorded.candidate("timeout")
    assert recorded.receive(headers, body) == "reconcile"

    def unavailable(name: str) -> SyntheticSignatureProvider:
        if provider == "unavailable":
            raise SignatureProviderUnavailableError
        return SyntheticSignatureProvider()

    monkeypatch.setattr(signing, "signature_provider_for", unavailable)
    assert recorded.reconcile(operation) == "failed"
    assert recorded.base.refresh(operation).failure_reason == "signature_invalid"


def test_abandon_during_verification_cannot_complete_the_candidate(
    recorded: RecordedSetup,
) -> None:
    operation, headers, body, expected = recorded.candidate("valid")
    api = recorded_api("valid", expected)
    graph = recorded.base.graph

    class AbandoningAPI:
        def verify(self, signed_bytes: bytes) -> bytes:
            with tenant_context(graph.physician, graph.organization_a):
                signing.abandon_signature(
                    clinic_id=graph.clinic_a, operation_id=operation.pk
                )
            return api.verify(signed_bytes)

    recorded.provider.verifier = SignatureVerificationAPIAdapter(AbandoningAPI())
    assert recorded.receive(headers, body) == "applied"
    final = recorded.base.refresh(operation)
    assert final.state == "failed"
    assert final.failure_reason == "abandoned"
    with runtime_role():
        assert signing.reconcile_signature(uuid4()) == "skipped"


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        (
            b'{"event_id":"event","operation_id":"operation","status":"signed",'
            b'"signed_bytes":"not-base64!"}'
        ),
        (
            b'{"event_id":"event","operation_id":"operation","status":"signed",'
            b'"signed_bytes":false}'
        ),
    ],
)
def test_raw_authenticated_callback_still_rejects_malformed_data(body: bytes) -> None:
    headers = {
        SIGNATURE_HEADER: hmac.new(
            provider_module.SYNTHETIC_SECRET, body, hashlib.sha256
        ).hexdigest()
    }
    with pytest.raises(SignatureCallbackError):
        SyntheticSignatureProvider().authenticate_callback(headers=headers, body=body)
