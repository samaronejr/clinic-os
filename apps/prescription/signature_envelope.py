"""Bounded synthetic certificate envelope; not a PAdES interoperability claim."""

from __future__ import annotations

import base64
import binascii
import json
from typing import TYPE_CHECKING

import rfc8785

from apps.prescription.signature_provider import (
    MAX_SIGNED_BYTES,
    SignatureVerificationError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

MARKER = b"\n%%SYNTHETIC-X509-SIGNATURE\n"
END = b"\n%%END-X509-SIGNATURE\n"
PROFILE = "clinic-synthetic-x509-v1"
DOCUMENT_DOMAIN = b"clinic-document-x509-v1\0"
TIMESTAMP_DOMAIN = b"clinic-timestamp-x509-v1\0"
BINDING_KEYS = frozenset(
    {"operation_ref", "content_digest", "signer_subject", "category", "approval_digest"}
)
STAMP_KEYS = frozenset({"time", "imprint", "signature", "certificate"})
MANIFEST_KEYS = frozenset(
    {"profile", "binding", "signature", "certificate", "intermediate", "timestamp"}
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate fields rather than silently accepting the last value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SignatureVerificationError
        result[key] = value
    return result


def closed_object(value: object, keys: frozenset[str]) -> dict[str, object]:
    """Validate one untrusted, closed JSON object."""
    if not isinstance(value, dict) or set(value) != keys:
        raise SignatureVerificationError
    return value


def json_object(raw: bytes, keys: frozenset[str]) -> dict[str, object]:
    """Decode bounded caller-provided JSON without duplicate fields."""
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as error:
        raise SignatureVerificationError from error
    return closed_object(value, keys)


def binary(value: object, *, maximum: int = 16384) -> bytes:
    """Decode a bounded certificate, signature or CRL from its exact base64."""
    if type(value) is not str or not 1 <= len(value) <= 4 * maximum:
        raise SignatureVerificationError
    try:
        result = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SignatureVerificationError from error
    if not 1 <= len(result) <= maximum:
        raise SignatureVerificationError
    return result


def encoded(value: bytes) -> str:
    """Encode exact bytes without textual normalization."""
    return base64.b64encode(value).decode("ascii")


def document_message(content: bytes, binding: Mapping[str, str]) -> bytes:
    """Bind the exact original bytes and every approval/signer/class field."""
    return DOCUMENT_DOMAIN + rfc8785.dumps(dict(binding)) + b"\0" + content


def timestamp_message(stamp: Mapping[str, str]) -> bytes:
    """Domain-separate the authenticated signature-imprint timestamp."""
    return TIMESTAMP_DOMAIN + rfc8785.dumps(dict(stamp))


def parse_envelope(signed_bytes: bytes) -> tuple[bytes, dict[str, object]]:
    """Refuse unsigned data, legacy envelopes and malformed certificate frames."""
    if not 1 <= len(signed_bytes) <= MAX_SIGNED_BYTES or not signed_bytes.endswith(END):
        raise SignatureVerificationError
    marker = signed_bytes.rfind(MARKER)
    if marker < 0 or not signed_bytes.startswith(b"%PDF-"):
        raise SignatureVerificationError
    content = signed_bytes[:marker]
    manifest = json_object(
        signed_bytes[marker + len(MARKER) : -len(END)], MANIFEST_KEYS
    )
    if manifest["profile"] != PROFILE:
        raise SignatureVerificationError
    return content, manifest
