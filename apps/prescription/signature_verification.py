"""Independent recorded verification API; no qualified trust is claimed.

The normalized API response is recorded in ADR-010. Its policy and
provenance are deliberately synthetic, not an ICP-Brasil certificate
validation. No response from this adapter may authorize live issuance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from django.db import connection
from django.utils import timezone
from django.utils.timezone import now as utc_now

from apps.prescription.signature_provider import (
    SignatureProviderTransientError,
    SignatureProviderUnavailableError,
    SignatureVerificationError,
    VerifiedSignature,
    _require_synthetic,
)

_RESPONSE_KEYS = frozenset(
    {
        "schema_version",
        "provenance",
        "policy",
        "format",
        "signed_digest",
        "content_digest",
        "signer_subject",
        "operation_ref",
        "category",
        "approval_digest",
        "chain",
        "timestamp",
        "revocation",
        "signed_at",
        "checked_at",
    }
)
_MAX_RESPONSE_BYTES = 8192


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
    """Verification service distinct from the signing partner."""

    def verify(self, signed_bytes: bytes) -> bytes:
        """Return a normalized artifact-bound report or a typed timeout."""
        ...


@dataclass(frozen=True, slots=True)
class RecordedVerificationAPI:
    """Replay one pinned synthetic response for exactly one artifact digest."""

    response: bytes | None
    signed_digest: str

    def verify(self, signed_bytes: bytes) -> bytes:
        """Never manufacture a positive response for a different artifact."""
        _require_synthetic()
        if sha256(signed_bytes).hexdigest() != self.signed_digest:
            raise SignatureVerificationError
        if self.response is None:
            raise SignatureProviderTransientError
        return self.response


def _response_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate response fields instead of silently accepting the last."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SignatureVerificationError
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class SignatureVerificationAPIAdapter:
    """Validate independent API evidence against an immutable stored approval."""

    api: VerificationAPI

    def verify(self, request: VerificationRequest) -> VerifiedSignature:
        """Require chain, timestamp AND revocation plus every exact binding."""
        _require_synthetic()
        if connection.in_atomic_block:
            raise SignatureProviderUnavailableError
        raw = self.api.verify(request.signed_bytes)
        if not 1 <= len(raw) <= _MAX_RESPONSE_BYTES:
            raise SignatureVerificationError
        try:
            report = json.loads(raw, object_pairs_hook=_response_object)
        except (ValueError, UnicodeError) as error:
            raise SignatureVerificationError from error
        if not isinstance(report, dict) or set(report) != _RESPONSE_KEYS:
            raise SignatureVerificationError
        expected = {
            "schema_version": 1,
            "provenance": "synthetic-fixture",
            "policy": "recorded-rehearsal-v1",
            "format": "synthetic-envelope",
            "signed_digest": sha256(request.signed_bytes).hexdigest(),
            "content_digest": request.content_digest,
            "signer_subject": request.signer_subject,
            "operation_ref": request.operation_ref,
            "category": request.category,
            "approval_digest": request.approval_digest,
            "chain": "trusted",
            "timestamp": "valid",
            "revocation": "good",
        }
        if any(
            type(report[key]) is not type(value) or report[key] != value
            for key, value in expected.items()
        ):
            raise SignatureVerificationError
        signed_at = _report_time(report["signed_at"])
        checked_at = _report_time(report["checked_at"])
        if not request.not_before <= signed_at <= checked_at <= utc_now():
            raise SignatureVerificationError
        return VerifiedSignature(signed_at=signed_at)


def _report_time(value: object) -> datetime:
    """Accept timezone-aware timestamps only, without reflecting report text."""
    if type(value) is not str:
        raise SignatureVerificationError
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise SignatureVerificationError from error
    if timezone.is_naive(result):
        raise SignatureVerificationError
    return result
