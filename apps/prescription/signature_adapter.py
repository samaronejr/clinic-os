"""Recorded signing adapter composed with a separate verification API.

All artifacts and responses are synthetic. This adapter exercises the
future cloud-certificate boundary, not a real PAdES/ICP-Brasil integration.
It is supplied explicitly by a rehearsal harness; the legacy synthetic
provider remains the application default.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.prescription.signature_provider import (
    SYNTHETIC_END,
    SYNTHETIC_MARKER,
    SignatureAcceptance,
    SignatureCallbackError,
    SignatureCallbackFacts,
    SignatureProviderRejectedError,
    SignatureRequest,
    SignatureVerificationError,
    SyntheticSignatureProvider,
    VerifiedSignature,
    _require_synthetic,
    signature_approval_digest,
)
from apps.prescription.signature_verification import (
    SignatureVerificationAPIAdapter,
    VerificationRequest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

RECORDED_PROVIDER = "recorded-signature-v1"


class RecordedSignatureProvider:
    """Keep signing and independent verification on distinct objects."""

    provider = RECORDED_PROVIDER
    synthetic = True

    def __init__(self, verifier: SignatureVerificationAPIAdapter) -> None:
        """Inject recorded verification evidence independently of the signer."""
        self.verifier = verifier
        self._signer = SyntheticSignatureProvider()
        self._requests: dict[str, SignatureRequest] = {}
        self._callbacks: dict[str, SignatureCallbackFacts] = {}

    def prepare(self, request: SignatureRequest) -> SignatureAcceptance:
        """Accept only the exact reviewed synthetic bytes, signer and class."""
        _require_synthetic()
        if (
            request.category != "synthetic_non_controlled"
            or request.document_version < 1
            or request.approval_digest != signature_approval_digest(request)
        ):
            raise SignatureProviderRejectedError
        acceptance = self._signer.begin(request)
        self._requests[acceptance.operation_ref] = request
        return acceptance

    def begin(self, request: SignatureRequest) -> SignatureAcceptance:
        """Adapt the existing dispatch contract without changing its callers."""
        return self.prepare(request)

    def sign(
        self, request: SignatureRequest, operation_ref: str
    ) -> tuple[dict[str, str], bytes]:
        """Complete only the previously prepared exact request in this fixture."""
        if self._requests.get(operation_ref) != request:
            raise SignatureProviderRejectedError
        headers, body = self._signer.sign(request, operation_ref)
        self._callbacks[operation_ref] = self.authenticate_callback(
            headers=headers, body=body
        )
        return headers, body

    def status(self, operation_ref: str) -> SignatureCallbackFacts | None:
        """Read the recorded operation result without another signing attempt."""
        _require_synthetic()
        return self._callbacks.get(operation_ref)

    def authenticate_callback(
        self, *, headers: Mapping[str, str], body: bytes
    ) -> SignatureCallbackFacts:
        """Authenticate raw bytes AND correlate the envelope before DB lookup."""
        facts = self._signer.authenticate_callback(headers=headers, body=body)
        if facts.status == "signed":
            marker = facts.signed_bytes.rfind(SYNTHETIC_MARKER)
            if marker < 0 or not facts.signed_bytes.endswith(SYNTHETIC_END):
                raise SignatureCallbackError
            raw = facts.signed_bytes[
                marker + len(SYNTHETIC_MARKER) : -len(SYNTHETIC_END)
            ]
            try:
                manifest = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise SignatureCallbackError from error
            if (
                not isinstance(manifest, dict)
                or manifest.get("operation_id") != facts.operation_ref
            ):
                raise SignatureCallbackError
        return facts

    def verify(self, request: VerificationRequest) -> VerifiedSignature:
        """Delegate to the independent verifier, never the signer's checker."""
        return self.verifier.verify(request)

    def verify_signature(
        self,
        *,
        signed_bytes: bytes,
        content_digest: str,
        signer_subject: str,
        operation_ref: str,
        not_before: datetime,
    ) -> VerifiedSignature:
        """Support the old method with the same independently bound evidence."""
        prepared = self._requests.get(operation_ref)
        if (
            prepared is None
            or prepared.content_digest != content_digest
            or prepared.signer_subject != signer_subject
            or prepared.category is None
        ):
            raise SignatureVerificationError
        return self.verify(
            VerificationRequest(
                signed_bytes=signed_bytes,
                content_digest=content_digest,
                signer_subject=signer_subject,
                operation_ref=operation_ref,
                category=prepared.category,
                approval_digest=prepared.approval_digest,
                not_before=not_before,
            )
        )
