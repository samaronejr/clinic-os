"""Explicit synthetic certificate signing material for the rehearsal harness."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

import rfc8785
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from apps.prescription.signature_envelope import (
    END,
    MARKER,
    PROFILE,
    document_message,
    encoded,
    timestamp_message,
)
from apps.prescription.signature_provider import (
    SignatureProviderRejectedError,
    SignatureRequest,
    _require_synthetic,
)

if TYPE_CHECKING:
    from datetime import datetime


def _sign(private_bytes: bytes, message: bytes) -> bytes:
    """Sign only with explicitly supplied, parseable RSA test material."""
    try:
        private = serialization.load_pem_private_key(private_bytes, password=None)
    except (ValueError, TypeError) as error:
        raise SignatureProviderRejectedError from error
    if not isinstance(private, rsa.RSAPrivateKey):
        raise SignatureProviderRejectedError
    return private.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


@dataclass(frozen=True, slots=True)
class CertificateSigningMaterial:
    """Public test private keys are supplied explicitly, never loaded by default."""

    private_key: bytes
    certificate: bytes
    intermediate: bytes
    timestamp_key: bytes
    timestamp_certificate: bytes

    def sign(
        self, request: SignatureRequest, operation_ref: str, signed_at: datetime
    ) -> bytes:
        """Sign exact content/binding and obtain a cryptographic timestamp proof."""
        _require_synthetic()
        if request.category is None:
            raise SignatureProviderRejectedError
        binding = {
            "operation_ref": operation_ref,
            "content_digest": request.content_digest,
            "signer_subject": request.signer_subject,
            "category": request.category,
            "approval_digest": request.approval_digest,
        }
        signature = _sign(self.private_key, document_message(request.content, binding))
        stamp = {
            "time": signed_at.isoformat(),
            "imprint": sha256(signature).hexdigest(),
        }
        manifest: dict[str, str | dict[str, str]] = {
            "profile": PROFILE,
            "binding": binding,
            "signature": encoded(signature),
            "certificate": encoded(self.certificate),
            "intermediate": encoded(self.intermediate),
            "timestamp": {
                **stamp,
                "certificate": encoded(self.timestamp_certificate),
                "signature": encoded(
                    _sign(self.timestamp_key, timestamp_message(stamp))
                ),
            },
        }
        return request.content + MARKER + rfc8785.dumps(manifest) + END
