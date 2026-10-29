"""Synthetic certificate signer composed with independent local verification.

All artifacts and responses are synthetic. This adapter exercises the
future cloud-certificate boundary, not a real PAdES/ICP-Brasil integration.
It is supplied explicitly by a rehearsal harness; the legacy synthetic
provider remains the application default.
"""

from __future__ import annotations

import base64
import json
import secrets
from hashlib import sha256
from typing import TYPE_CHECKING

from django.utils.timezone import now as utc_now

from apps.prescription.signature_envelope import (
    BINDING_KEYS,
    closed_object,
    parse_envelope,
)
from apps.prescription.signature_provider import (
    SIGNATURE_HEADER,
    SignatureAcceptance,
    SignatureCallbackError,
    SignatureCallbackFacts,
    SignatureProviderRejectedError,
    SignatureProviderUnavailableError,
    SignatureRequest,
    SignatureVerificationError,
    SyntheticSignatureProvider,
    VerifiedSignature,
    _callback_signature,
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

    from apps.prescription.signature_certificate import CertificateSigningMaterial

RECORDED_PROVIDER = "recorded-signature-v1"


class RecordedSignatureProvider:
    """Keep private-key signing and public-key/CRL verification distinct."""

    provider = RECORDED_PROVIDER
    synthetic = True

    def __init__(
        self,
        verifier: SignatureVerificationAPIAdapter,
        *,
        material: CertificateSigningMaterial | None = None,
    ) -> None:
        """Inject recorded verification evidence independently of the signer."""
        self.verifier = verifier
        self.material = material
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
            or sha256(request.content).hexdigest() != request.content_digest
        ):
            raise SignatureProviderRejectedError
        if self.material is None:
            raise SignatureProviderUnavailableError
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
        if self.material is None:
            raise SignatureProviderUnavailableError
        signed_bytes = self.material.sign(request, operation_ref, utc_now())
        body = json.dumps(
            {
                "event_id": f"event-{secrets.token_hex(8)}",
                "operation_id": operation_ref,
                "status": "signed",
                "signed_bytes": base64.b64encode(signed_bytes).decode("ascii"),
            }
        ).encode()
        headers = {SIGNATURE_HEADER: _callback_signature(body)}
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
            try:
                _, manifest = parse_envelope(facts.signed_bytes)
                binding = closed_object(manifest["binding"], BINDING_KEYS)
            except SignatureVerificationError as error:
                raise SignatureCallbackError from error
            if binding["operation_ref"] != facts.operation_ref:
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
