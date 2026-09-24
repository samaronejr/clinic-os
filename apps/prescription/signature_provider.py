"""Signature provider boundary; no real signing capability is approved.

The task-6 register records ``qualified_signing`` and
``signature_verification`` as unavailable (2026-09-24-v2): no provider,
format profile, verifier package or sandbox is selected. This module pins
the provider contract and supplies an explicitly synthetic implementation
for the rehearsal lifecycle only. The synthetic provider signs with a fixed
test key, never a credential, and refuses to run outside synthetic data
mode. ``signature_capability().real_enabled`` is permanently ``False``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

import rfc8785
from django.conf import settings
from django.utils import timezone
from django.utils.timezone import now as utc_now

if TYPE_CHECKING:
    from uuid import UUID

type JsonValue = (
    bool | int | str | float | None | Sequence[JsonValue] | Mapping[str, JsonValue]
)

SYNTHETIC_PROVIDER = "synthetic-signature-v1"
SYNTHETIC_ENVELOPE_VERSION = "clinic-synthetic-signature-v1"
SYNTHETIC_MARKER = b"\n%%SYNTHETIC-SIGNATURE\n"
SYNTHETIC_END = b"\n%%END-SIGNATURE\n"
SYNTHETIC_SECRET = b"synthetic-signing-key-not-a-credential"
SIGNATURE_HEADER = "x-synthetic-signature"
MAX_CALLBACK_BYTES = 4 * 1024 * 1024
MAX_SIGNED_BYTES = 4 * 1024 * 1024
MAX_REFERENCE_LENGTH = 128
_MANIFEST_KEYS = frozenset(
    {"v", "operation_id", "content_digest", "signer", "signed_at", "signature"}
)
_CALLBACK_KEYS = frozenset({"event_id", "operation_id", "status", "signed_bytes"})


class SignatureProviderError(Exception):
    """Base error for the signing provider boundary."""


class SignatureProviderUnavailableError(SignatureProviderError):
    """Report that no approved signing capability is usable here."""


class SignatureProviderTransientError(SignatureProviderError):
    """Report a retryable provider failure such as a timeout or outage."""


class SignatureProviderRejectedError(SignatureProviderError):
    """Report a non-retryable provider refusal of the signing request."""


class SignatureCallbackError(SignatureProviderError):
    """Reject a callback that fails provider authentication."""

    def __init__(self) -> None:
        """Expose one stable non-identifying rejection message."""
        super().__init__("signature callback authentication failed")


class SignatureVerificationError(SignatureProviderError):
    """Report signed bytes that fail independent verification."""


@dataclass(frozen=True, slots=True)
class SignatureRequest:
    """Exact signing input: stored operation, issuer, signer and bytes."""

    operation_id: UUID
    issuer_id: UUID
    signer_subject: str
    content_digest: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SignatureAcceptance:
    """Provider-side operation reference; never a signed result."""

    operation_ref: str


@dataclass(frozen=True, slots=True)
class SignatureCallbackFacts:
    """Verified callback facts; unauthenticated claims are never carried."""

    event_id: str
    operation_ref: str
    status: Literal["signed", "failed"]
    signed_bytes: bytes
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class VerifiedSignature:
    """Validated signing evidence; issuance time comes only from here."""

    signed_at: datetime


@dataclass(frozen=True, slots=True)
class SignatureCapability:
    """Explicit synthetic opt-in with a permanently closed live gate."""

    synthetic_enabled: bool
    real_enabled: bool = False
    reason: str = "missing_signing_provider_and_owner_approval"


def signature_capability() -> SignatureCapability:
    """Reflect the unavailable task-6 record, not caller claims of approval."""
    return SignatureCapability(
        synthetic_enabled=getattr(settings, "PRESCRIPTION_SYNTHETIC_SIGNING", False)
        is True
    )


class SignatureProvider(Protocol):
    """Contract an approved signing provider must satisfy.

    ``begin`` runs outside the tenant transaction and returns only an
    operation reference. ``authenticate_callback`` verifies the raw
    provider callback before any stored operation or tenant is resolved.
    ``verify_signature`` independently validates the returned signed bytes
    against the expected content digest and signer; a provider-reported
    success status is never sufficient for issuance.
    """

    provider: str

    def begin(self, request: SignatureRequest) -> SignatureAcceptance:
        """Open the provider-side signing operation or raise a typed error."""
        ...

    def authenticate_callback(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> SignatureCallbackFacts:
        """Verify the provider signature and return only verified fields."""
        ...

    def verify_signature(
        self,
        *,
        signed_bytes: bytes,
        content_digest: str,
        signer_subject: str,
        operation_ref: str,
        not_before: datetime,
    ) -> VerifiedSignature:
        """Verify the signed artifact or raise SignatureVerificationError."""
        ...


def _require_synthetic() -> None:
    """Refuse every provider path outside the synthetic rehearsal."""
    if (
        settings.CLINIC_DATA_MODE != "synthetic"
        or not signature_capability().synthetic_enabled
    ):
        raise SignatureProviderUnavailableError


def _manifest_signature(manifest: Mapping[str, JsonValue]) -> str:
    """Compute the synthetic signature over the manifest minus itself."""
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return hmac.new(
        SYNTHETIC_SECRET, rfc8785.dumps(unsigned), hashlib.sha256
    ).hexdigest()


def _callback_signature(body: bytes) -> str:
    """Compute the synthetic callback authentication signature."""
    return hmac.new(SYNTHETIC_SECRET, body, hashlib.sha256).hexdigest()


def _callback_signed_bytes(payload: dict[str, object], status: str) -> bytes:
    """Decode signed bytes only for a signed report; reject stray fields."""
    if status != "signed":
        if "signed_bytes" in payload:
            raise SignatureCallbackError
        return b""
    encoded = payload.get("signed_bytes")
    if type(encoded) is not str:
        raise SignatureCallbackError
    try:
        signed_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SignatureCallbackError from error
    if not 1 <= len(signed_bytes) <= MAX_SIGNED_BYTES:
        raise SignatureCallbackError
    return signed_bytes


class SyntheticSignatureProvider:
    """Deterministic synthetic signer; never a certificate or a credential.

    The signed artifact is ``content + marker + canonical manifest + end``:
    the original rendered bytes are preserved verbatim as the envelope
    prefix, and the manifest binds the exact content digest, signer subject,
    provider operation reference and signing time. ``sign`` simulates the
    provider completing the operation and emitting its authenticated
    callback; tests drive it, production code never calls it.
    """

    provider = SYNTHETIC_PROVIDER

    def begin(self, request: SignatureRequest) -> SignatureAcceptance:
        """Accept only visibly synthetic requests bound to exact bytes."""
        _require_synthetic()
        if (
            not request.signer_subject.startswith("synthetic:")
            or hashlib.sha256(request.content).hexdigest() != request.content_digest
        ):
            raise SignatureProviderRejectedError
        return SignatureAcceptance(operation_ref=f"synop-{secrets.token_hex(16)}")

    def sign(
        self, request: SignatureRequest, operation_ref: str
    ) -> tuple[dict[str, str], bytes]:
        """Produce the signed artifact and its authenticated callback frame."""
        _require_synthetic()
        manifest: dict[str, JsonValue] = {
            "v": SYNTHETIC_ENVELOPE_VERSION,
            "operation_id": operation_ref,
            "content_digest": request.content_digest,
            "signer": request.signer_subject,
            "signed_at": utc_now().isoformat(),
        }
        manifest["signature"] = _manifest_signature(manifest)
        signed_bytes = (
            request.content + SYNTHETIC_MARKER + rfc8785.dumps(manifest) + SYNTHETIC_END
        )
        payload: dict[str, object] = {
            "event_id": f"event-{secrets.token_hex(8)}",
            "operation_id": operation_ref,
            "status": "signed",
            "signed_bytes": base64.b64encode(signed_bytes).decode("ascii"),
        }
        body = json.dumps(payload).encode()
        return {SIGNATURE_HEADER: _callback_signature(body)}, body

    def authenticate_callback(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> SignatureCallbackFacts:
        """Authenticate the raw callback before any operation is resolved."""
        _require_synthetic()
        if len(body) > MAX_CALLBACK_BYTES:
            raise SignatureCallbackError
        signature = headers.get(SIGNATURE_HEADER, "")
        if not hmac.compare_digest(signature, _callback_signature(body)):
            raise SignatureCallbackError
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise SignatureCallbackError from error
        if not isinstance(payload, dict) or set(payload) - _CALLBACK_KEYS:
            raise SignatureCallbackError
        event_id = payload.get("event_id")
        operation_ref = payload.get("operation_id")
        status = payload.get("status")
        if (
            type(event_id) is not str
            or not 1 <= len(event_id) <= MAX_REFERENCE_LENGTH
            or type(operation_ref) is not str
            or not 1 <= len(operation_ref) <= MAX_REFERENCE_LENGTH
            or status not in ("signed", "failed")
        ):
            raise SignatureCallbackError
        signed_bytes = _callback_signed_bytes(payload, status)
        return SignatureCallbackFacts(
            event_id=event_id,
            operation_ref=operation_ref,
            status=status,
            signed_bytes=signed_bytes,
            payload=payload,
        )

    def verify_signature(
        self,
        *,
        signed_bytes: bytes,
        content_digest: str,
        signer_subject: str,
        operation_ref: str,
        not_before: datetime,
    ) -> VerifiedSignature:
        """Verify envelope, digest, signer, operation and signing time."""
        _require_synthetic()
        if not signed_bytes.endswith(SYNTHETIC_END):
            raise SignatureVerificationError
        marker_at = signed_bytes.rfind(SYNTHETIC_MARKER)
        if marker_at < 0:
            raise SignatureVerificationError
        content = signed_bytes[:marker_at]
        manifest_raw = signed_bytes[
            marker_at + len(SYNTHETIC_MARKER) : -len(SYNTHETIC_END)
        ]
        try:
            manifest = json.loads(manifest_raw)
        except json.JSONDecodeError as error:
            raise SignatureVerificationError from error
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
            raise SignatureVerificationError
        if (
            manifest["v"] != SYNTHETIC_ENVELOPE_VERSION
            or manifest["operation_id"] != operation_ref
            or manifest["signer"] != signer_subject
            or manifest["content_digest"] != content_digest
            or hashlib.sha256(content).hexdigest() != content_digest
            or not hmac.compare_digest(
                str(manifest["signature"]), _manifest_signature(manifest)
            )
        ):
            raise SignatureVerificationError
        signed_at_raw = manifest["signed_at"]
        if type(signed_at_raw) is not str:
            raise SignatureVerificationError
        try:
            signed_at = datetime.fromisoformat(signed_at_raw)
        except ValueError as error:
            raise SignatureVerificationError from error
        if (
            timezone.is_naive(signed_at)
            or signed_at < not_before
            or signed_at > utc_now()
        ):
            raise SignatureVerificationError
        return VerifiedSignature(signed_at=signed_at)


def signature_provider_for(provider: str) -> SignatureProvider:
    """Resolve a stored provider name; unknown or disabled names fail closed."""
    if provider == SYNTHETIC_PROVIDER and signature_capability().synthetic_enabled:
        return SyntheticSignatureProvider()
    raise SignatureProviderUnavailableError


def get_signature_provider() -> SignatureProvider:
    """Resolve the only explicitly enabled provider for new operations."""
    return signature_provider_for(SYNTHETIC_PROVIDER)
