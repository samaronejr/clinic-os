"""T35-VERIFY: real public-key checks against fixed, independently signed vectors."""

from __future__ import annotations

import base64
import json
import socket
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from apps.prescription.signature_adapter import RecordedSignatureProvider
from apps.prescription.signature_envelope import END, MARKER, parse_envelope
from apps.prescription.signature_provider import (
    SignatureProviderRejectedError,
    SignatureProviderTransientError,
    SignatureProviderUnavailableError,
    SignatureRequest,
    SignatureVerificationError,
    signature_approval_digest,
)
from apps.prescription.signature_verification import (
    RecordedVerificationAPI,
    SignatureVerificationAPIAdapter,
)
from cryptography.hazmat.primitives import serialization
from django.db.backends.base.base import BaseDatabaseWrapper

from prescription.test_signature_adapter import (
    ANCHOR,
    EXPECTATION,
    PKI,
    material,
    recorded_api,
    synthetic_verification,
    verification_adapter,
)
from renewal.test_signatures import NOW

__all__ = ("synthetic_verification",)
pytestmark = pytest.mark.django_db(transaction=True)


def rebuilt(content: bytes, manifest: dict[str, object]) -> bytes:
    return content + MARKER + json.dumps(manifest).encode() + END


def test_unsigned_bytes_cannot_be_promoted_by_the_old_invented_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identical offline probe accepted this on parent 28a7c0f (exit 1)."""
    unsigned = b"THIS IS NOT A PDF OR A SIGNATURE"
    request = replace(
        EXPECTATION,
        signed_bytes=unsigned,
        content_digest=sha256(b"different approved content").hexdigest(),
    )
    invented = {
        "schema_version": 1,
        "provenance": "synthetic-fixture",
        "policy": "recorded-rehearsal-v1",
        "format": "synthetic-envelope",
        "signed_digest": sha256(unsigned).hexdigest(),
        "content_digest": request.content_digest,
        "signer_subject": request.signer_subject,
        "operation_ref": request.operation_ref,
        "category": request.category,
        "approval_digest": request.approval_digest,
        "chain": "trusted",
        "timestamp": "valid",
        "revocation": "good",
        "signed_at": NOW.isoformat(),
        "checked_at": NOW.isoformat(),
    }
    verifier = verification_adapter(
        RecordedVerificationAPI(
            response=json.dumps(invented).encode(),
            signed_digest=sha256(unsigned).hexdigest(),
        )
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("The offline unsigned-artifact probe reached DB/network")

    with monkeypatch.context() as patch:
        patch.setattr(BaseDatabaseWrapper, "ensure_connection", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(socket.socket, "connect_ex", forbidden)
        with pytest.raises(SignatureVerificationError):
            verifier.verify(request)


def test_fixed_positive_vector_verifies_without_network_database_or_private_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Independent verification tried to load a private key or use IO")

    with monkeypatch.context() as patch:
        patch.setattr(BaseDatabaseWrapper, "ensure_connection", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(socket.socket, "connect_ex", forbidden)
        patch.setattr(serialization, "load_pem_private_key", forbidden)
        assert (
            verification_adapter(recorded_api("valid", EXPECTATION))
            .verify(EXPECTATION)
            .signed_at
            == NOW
        )


def test_wrong_configured_anchor_cannot_validate_the_fixed_signature() -> None:
    wrong_anchor = base64.b64decode(PKI["certificates"]["wrong_root"])
    verifier = SignatureVerificationAPIAdapter(
        recorded_api("valid", EXPECTATION), trust_anchor=wrong_anchor
    )
    with pytest.raises(SignatureVerificationError):
        verifier.verify(EXPECTATION)
    assert wrong_anchor != ANCHOR


def test_missing_configured_anchor_fails_closed() -> None:
    with pytest.raises(SignatureProviderUnavailableError):
        SignatureVerificationAPIAdapter(recorded_api("valid", EXPECTATION)).verify(
            EXPECTATION
        )


def test_document_signature_covers_the_actual_bytes_not_just_claimed_digests() -> None:
    content, manifest = parse_envelope(EXPECTATION.signed_bytes)
    content += b"Synthetic changed document bytes"
    digest = sha256(content).hexdigest()
    assert isinstance(manifest["binding"], dict)
    manifest["binding"]["content_digest"] = digest
    request = replace(
        EXPECTATION, signed_bytes=rebuilt(content, manifest), content_digest=digest
    )
    with pytest.raises(SignatureVerificationError):
        verification_adapter(recorded_api("valid", request)).verify(request)


def test_timestamp_signature_authenticates_even_equivalent_time_spellings() -> None:
    content, manifest = parse_envelope(EXPECTATION.signed_bytes)
    assert isinstance(manifest["timestamp"], dict)
    manifest["timestamp"]["time"] = NOW.isoformat().replace("+00:00", "Z")
    request = replace(EXPECTATION, signed_bytes=rebuilt(content, manifest))
    with pytest.raises(SignatureVerificationError):
        verification_adapter(recorded_api("valid", request)).verify(request)


def test_document_signer_certificate_cannot_be_used_as_timestamp_authority() -> None:
    content, manifest = parse_envelope(EXPECTATION.signed_bytes)
    assert isinstance(manifest["timestamp"], dict)
    manifest["timestamp"]["certificate"] = PKI["certificates"]["leaf"]
    request = replace(EXPECTATION, signed_bytes=rebuilt(content, manifest))
    with pytest.raises(SignatureVerificationError):
        verification_adapter(recorded_api("valid", request)).verify(request)


@pytest.mark.parametrize("crl_name", ["root_crl", "intermediate_crl"])
def test_crl_signatures_are_checked_cryptographically(crl_name: str) -> None:
    response = dict(PKI["revocation"])
    original = base64.b64decode(response[crl_name])
    changed = original[:-1] + bytes([original[-1] ^ 1])
    response[crl_name] = base64.b64encode(changed).decode("ascii")
    api = RecordedVerificationAPI(
        response=json.dumps(response).encode(),
        signed_digest=sha256(EXPECTATION.signed_bytes).hexdigest(),
    )
    with pytest.raises(SignatureVerificationError):
        verification_adapter(api).verify(EXPECTATION)


def test_swapping_real_crls_cannot_substitute_an_unrelated_issuer() -> None:
    response = {
        "root_crl": PKI["revocation"]["intermediate_crl"],
        "intermediate_crl": PKI["revocation"]["root_crl"],
    }
    api = RecordedVerificationAPI(
        response=json.dumps(response).encode(),
        signed_digest=sha256(EXPECTATION.signed_bytes).hexdigest(),
    )
    with pytest.raises(SignatureVerificationError):
        verification_adapter(api).verify(EXPECTATION)


def test_stale_revocation_material_is_unavailable_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.prescription.signature_verification.utc_now",
        lambda: NOW + timedelta(days=2),
    )
    with pytest.raises(SignatureProviderTransientError):
        verification_adapter(recorded_api("valid", EXPECTATION)).verify(EXPECTATION)


@pytest.mark.parametrize("field", ["signature", "certificate", "intermediate"])
@pytest.mark.parametrize(
    "invalid",
    ["", "not-base64!", False, "x" * 70000],
    ids=["empty", "bad-base64", "wrong-type", "oversized"],
)
def test_invalid_crypto_material_is_bounded_and_never_accepted(
    field: str, invalid: object
) -> None:
    content, manifest = parse_envelope(EXPECTATION.signed_bytes)
    manifest[field] = invalid
    request = replace(EXPECTATION, signed_bytes=rebuilt(content, manifest))
    with pytest.raises(SignatureVerificationError):
        verification_adapter(recorded_api("valid", request)).verify(request)


@pytest.mark.parametrize("shape", ["missing", "extra", "duplicate", "profile"])
def test_signature_envelope_schema_is_closed(shape: str) -> None:
    content, manifest = parse_envelope(EXPECTATION.signed_bytes)
    if shape == "missing":
        del manifest["signature"]
    if shape == "extra":
        manifest["trusted"] = True
    if shape == "profile":
        manifest["profile"] = "invented-good-verdict"
    raw = json.dumps(manifest).encode()
    if shape == "duplicate":
        raw = raw[:-1] + b',"signature":"duplicate"}'
    request = replace(EXPECTATION, signed_bytes=content + MARKER + raw + END)
    with pytest.raises(SignatureVerificationError):
        verification_adapter(recorded_api("valid", request)).verify(request)


def approved_request() -> SignatureRequest:
    content = b"%PDF-1.4\nSynthetic signing material boundary\n"
    request = SignatureRequest(
        operation_id=uuid4(),
        issuer_id=uuid4(),
        signer_subject=PKI["signer_subject"],
        content=content,
        content_digest=sha256(content).hexdigest(),
        category="synthetic_non_controlled",
        document_version=1,
    )
    return replace(request, approval_digest=signature_approval_digest(request))


def test_signing_material_is_explicit_but_not_needed_for_verification() -> None:
    provider = RecordedSignatureProvider(
        verification_adapter(recorded_api("valid", EXPECTATION))
    )
    with pytest.raises(SignatureProviderUnavailableError):
        provider.prepare(approved_request())


def test_signing_rejects_missing_material_after_prepare() -> None:
    provider = RecordedSignatureProvider(
        verification_adapter(recorded_api("valid", EXPECTATION)), material=material()
    )
    request = approved_request()
    acceptance = provider.prepare(request)
    provider.material = None
    with pytest.raises(SignatureProviderUnavailableError):
        provider.sign(request, acceptance.operation_ref)


def test_signer_refuses_unparseable_private_material() -> None:
    malformed = replace(material(), private_key=b"synthetic-not-a-private-key")
    with pytest.raises(SignatureProviderRejectedError):
        malformed.sign(approved_request(), "synthetic-operation", NOW)


def test_signer_refuses_an_unselected_class() -> None:
    request = replace(approved_request(), category=None)
    with pytest.raises(SignatureProviderRejectedError):
        material().sign(request, "synthetic-operation", NOW)
