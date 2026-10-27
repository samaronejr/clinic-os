"""Local X.509, exact-byte signature, timestamp and CRL verification.

The restricted sandbox policy uses the already locked cryptography library.
No verdict supplied by the signer or a recorded response is trusted. An
explicit configured synthetic anchor is required; live anchors remain closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import (
    ExtendedKeyUsageOID,
    ExtensionOID,
    NameOID,
    ObjectIdentifier,
    SignatureAlgorithmOID,
)
from django.db import connection
from django.utils import timezone
from django.utils.timezone import now as utc_now

from apps.prescription.signature_envelope import (
    BINDING_KEYS,
    STAMP_KEYS,
    binary,
    closed_object,
    document_message,
    json_object,
    parse_envelope,
    timestamp_message,
)
from apps.prescription.signature_provider import (
    SignatureProviderTransientError,
    SignatureProviderUnavailableError,
    SignatureVerificationError,
    VerifiedSignature,
    _require_synthetic,
)

DOCUMENT_PURPOSE = ObjectIdentifier("1.3.6.1.4.1.55555.35.1")
TIMESTAMP_SIGNER = "synthetic:fixture:timestamp-35"
_MIN_RSA_BITS = 2048
_MAX_RSA_BITS = 4096
_RSA_EXPONENT = 65537
_CRL_KEYS = frozenset({"root_crl", "intermediate_crl"})
_CA_CRITICAL = frozenset({ExtensionOID.BASIC_CONSTRAINTS, ExtensionOID.KEY_USAGE})
_LEAF_CRITICAL = _CA_CRITICAL | {
    ExtensionOID.EXTENDED_KEY_USAGE,
    ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
}
_CRYPTO_ERRORS = (
    InvalidSignature,
    UnsupportedAlgorithm,
    ValueError,
    TypeError,
    x509.ExtensionNotFound,
    x509.DuplicateExtension,
    x509.UnsupportedGeneralNameType,
    x509.InvalidVersion,
)
type VerifiedChain = tuple[
    x509.Certificate, x509.Certificate, x509.Certificate, x509.Certificate
]


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """The stored approval and exact candidate bytes, never callback claims."""

    signed_bytes: bytes
    content_digest: str
    signer_subject: str
    operation_ref: str
    category: str
    approval_digest: str
    not_before: datetime


class VerificationAPI(Protocol):
    """Existing transport seam now supplies CRLs, never verification verdicts."""

    def verify(self, signed_bytes: bytes) -> bytes:
        """Return signed root/intermediate CRLs or a typed unavailability."""
        ...


@dataclass(frozen=True, slots=True)
class RecordedVerificationAPI:
    """Replay revocation-source bytes; these still require cryptographic checks."""

    response: bytes | None
    signed_digest: str

    def verify(self, signed_bytes: bytes) -> bytes:
        """Treat a missing revocation source as unavailable, never as trust."""
        _require_synthetic()
        if sha256(signed_bytes).hexdigest() != self.signed_digest:
            raise SignatureVerificationError
        if self.response is None:
            raise SignatureProviderTransientError
        return self.response


def _key(cert: x509.Certificate) -> rsa.RSAPublicKey:
    """Refuse unsupported or weak public keys before verification."""
    key = cert.public_key()
    if (
        not isinstance(key, rsa.RSAPublicKey)
        or not _MIN_RSA_BITS <= key.key_size <= _MAX_RSA_BITS
        or key.public_numbers().e != _RSA_EXPONENT
    ):
        raise SignatureVerificationError
    return key


def _certificate(
    cert: x509.Certificate, signed_at: datetime, *, path_length: int | None
) -> rsa.RSAPublicKey:
    """Enforce the restricted, explicit sandbox certificate profile."""
    ca = path_length is not None
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    allowed = _CA_CRITICAL if ca else _LEAF_CRITICAL
    if (
        cert.signature_algorithm_oid != SignatureAlgorithmOID.RSA_WITH_SHA256
        or not cert.not_valid_before_utc <= signed_at <= cert.not_valid_after_utc
        or not constraints.critical
        or not usage.critical
        or constraints.value.ca != ca
        or constraints.value.path_length != path_length
        or usage.value.key_cert_sign != ca
        or usage.value.crl_sign != ca
        or usage.value.digital_signature == ca
        or any(ext.critical and ext.oid not in allowed for ext in cert.extensions)
    ):
        raise SignatureVerificationError
    return _key(cert)


def _issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> None:
    """Verify actual certificate signatures, not just matching issuer names."""
    cert.verify_directly_issued_by(issuer)
    authority = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    subject = issuer.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    if authority.value.key_identifier != subject.value.digest:
        raise SignatureVerificationError


def _leaf(cert: x509.Certificate, *, subject: str, purpose: ObjectIdentifier) -> None:
    """Bind the certificate identity and purpose independently of signed claims."""
    identity = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    if (
        identity.value.get_values_for_type(x509.UniformResourceIdentifier) != [subject]
        or len(identity.value) != 1
        or not usage.critical
        or list(usage.value) != [purpose]
    ):
        raise SignatureVerificationError


def _verify(key: rsa.RSAPublicKey, signature: bytes, message: bytes) -> None:
    """Verify a domain-separated RSA-PSS/SHA-256 signature."""
    key.verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


def _time(value: object) -> datetime:
    """Require an aware timestamp before using its authenticated claim."""
    if type(value) is not str:
        raise SignatureVerificationError
    result = datetime.fromisoformat(value)
    if timezone.is_naive(result):
        raise SignatureVerificationError
    return result


def _crl(
    raw: object, issuer: x509.Certificate, checked_at: datetime
) -> x509.CertificateRevocationList:
    """Verify a direct, issuer-signed CRL and its current availability window."""
    crl = x509.load_der_x509_crl(binary(raw, maximum=65536))
    authority = crl.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    subject = issuer.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    if (
        crl.issuer != issuer.subject
        or crl.signature_algorithm_oid != SignatureAlgorithmOID.RSA_WITH_SHA256
        or not crl.is_signature_valid(_key(issuer))
        or authority.value.key_identifier != subject.value.digest
        or any(ext.critical for ext in crl.extensions)
    ):
        raise SignatureVerificationError
    if (
        crl.next_update_utc is None
        or not crl.last_update_utc <= checked_at < crl.next_update_utc
    ):
        raise SignatureProviderTransientError
    return crl


def _chain(
    anchor: bytes,
    manifest: dict[str, object],
    request: VerificationRequest,
    signed_at: datetime,
) -> VerifiedChain:
    """Validate the fixed-depth chain and distinct document/timestamp purposes."""
    root = x509.load_der_x509_certificate(anchor)
    names = root.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if (
        len(names) != 1
        or type(names[0].value) is not str
        or not names[0].value.startswith("Synthetic ")
    ):
        raise SignatureVerificationError
    intermediate = x509.load_der_x509_certificate(binary(manifest["intermediate"]))
    signer = x509.load_der_x509_certificate(binary(manifest["certificate"]))
    stamp = closed_object(manifest["timestamp"], STAMP_KEYS)
    timestamp = x509.load_der_x509_certificate(binary(stamp["certificate"]))
    _certificate(root, signed_at, path_length=1)
    _issued_by(root, root)
    _certificate(intermediate, signed_at, path_length=0)
    _issued_by(intermediate, root)
    _certificate(signer, signed_at, path_length=None)
    _issued_by(signer, intermediate)
    _leaf(signer, subject=request.signer_subject, purpose=DOCUMENT_PURPOSE)
    _certificate(timestamp, signed_at, path_length=None)
    _issued_by(timestamp, intermediate)
    _leaf(
        timestamp, subject=TIMESTAMP_SIGNER, purpose=ExtendedKeyUsageOID.TIME_STAMPING
    )
    return root, intermediate, signer, timestamp


@dataclass(frozen=True, slots=True)
class SignatureVerificationAPIAdapter:
    """Preserve the API seam while performing all trust decisions locally."""

    api: VerificationAPI
    trust_anchor: bytes | None = None

    def verify(self, request: VerificationRequest) -> VerifiedSignature:
        """Cryptographically verify exact bytes, chain, timestamp and revocation."""
        _require_synthetic()
        if connection.in_atomic_block:
            raise SignatureProviderUnavailableError
        try:
            return self._verify(request)
        except _CRYPTO_ERRORS as error:
            raise SignatureVerificationError from error

    def _verify(self, request: VerificationRequest) -> VerifiedSignature:
        content, manifest = parse_envelope(request.signed_bytes)
        binding = closed_object(manifest["binding"], BINDING_KEYS)
        expected = {
            "content_digest": request.content_digest,
            "signer_subject": request.signer_subject,
            "operation_ref": request.operation_ref,
            "category": request.category,
            "approval_digest": request.approval_digest,
        }
        if binding != expected or sha256(content).hexdigest() != request.content_digest:
            raise SignatureVerificationError
        stamp = closed_object(manifest["timestamp"], STAMP_KEYS)
        signed_at = _time(stamp["time"])
        checked_at = utc_now()
        signature = binary(manifest["signature"])
        if (
            stamp["imprint"] != sha256(signature).hexdigest()
            or not request.not_before <= signed_at <= checked_at
        ):
            raise SignatureVerificationError
        if self.trust_anchor is None:
            raise SignatureProviderUnavailableError
        chain = _chain(self.trust_anchor, manifest, request, signed_at)
        _verify(_key(chain[2]), signature, document_message(content, expected))
        time_claim = {"time": str(stamp["time"]), "imprint": str(stamp["imprint"])}
        _verify(
            _key(chain[3]), binary(stamp["signature"]), timestamp_message(time_claim)
        )
        self._revocation(request.signed_bytes, chain, checked_at)
        return VerifiedSignature(signed_at=signed_at)

    def _revocation(
        self,
        signed_bytes: bytes,
        chain: VerifiedChain,
        checked_at: datetime,
    ) -> None:
        raw = self.api.verify(signed_bytes)
        if not 1 <= len(raw) <= 256 * 1024:
            raise SignatureVerificationError
        evidence = json_object(raw, _CRL_KEYS)
        root, intermediate, signer, timestamp = chain
        root_crl = _crl(evidence["root_crl"], root, checked_at)
        leaf_crl = _crl(evidence["intermediate_crl"], intermediate, checked_at)
        for cert, crl in [
            (intermediate, root_crl),
            (signer, leaf_crl),
            (timestamp, leaf_crl),
        ]:
            if (
                crl.get_revoked_certificate_by_serial_number(cert.serial_number)
                is not None
            ):
                raise SignatureVerificationError
