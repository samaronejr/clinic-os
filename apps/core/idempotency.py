"""Canonical version-one create-idempotency fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping

type CreateKind = Literal["patient", "availability", "appointment", "invoice"]

DOMAIN: Final = b"clinic-idempotency-v1\0"
MAX_PATIENT_NAME_LENGTH: Final = 255
UTC_MINUTE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00Z")
MINOR_AMOUNT: Final = re.compile(r"[1-9][0-9]*")
CURRENCY: Final = re.compile(r"[A-Z]{3}")
EXPECTED_FIELDS: Final[dict[CreateKind, tuple[str, ...]]] = {
    "patient": ("birth_date", "clinic_id", "full_name"),
    "availability": ("clinic_id", "end_utc", "practitioner_id", "start_utc"),
    "appointment": (
        "clinic_id",
        "end_utc",
        "enrollment_id",
        "practitioner_id",
        "start_utc",
    ),
    "invoice": ("amount_minor", "clinic_id", "currency", "patient_id"),
}


class IdempotencyValueError(ValueError):
    """Reject noncanonical create-idempotency input."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("create-idempotency input is not canonical")


class PatientNameValueError(ValueError):
    """Reject a patient name that cannot be normalized safely."""


def normalize_patient_name(value: str) -> str:
    """Return a bounded NFC name with Unicode whitespace collapsed."""
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise PatientNameValueError
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not 1 <= len(normalized) <= MAX_PATIENT_NAME_LENGTH:
        raise PatientNameValueError
    return normalized


def canonical_create_payload(kind: CreateKind, values: Mapping[str, str]) -> bytes:
    """Encode one exact string-only create object as canonical UTF-8 JSON."""
    expected = EXPECTED_FIELDS.get(kind)
    if expected is None or tuple(sorted(values)) != expected:
        raise IdempotencyValueError
    for field in expected:
        value = values[field]
        if not isinstance(value, str) or not _canonical_scalar(field, value):
            raise IdempotencyValueError
    try:
        return json.dumps(
            dict(values),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError as error:
        raise IdempotencyValueError from error


def create_fingerprint(kind: CreateKind, values: Mapping[str, str]) -> bytes:
    """Hash the domain separator and exact canonical create payload."""
    return hashlib.sha256(DOMAIN + canonical_create_payload(kind, values)).digest()


def _canonical_scalar(field: str, value: str) -> bool:
    if field.endswith("_id"):
        return _canonical_uuid(value)
    if field.endswith("_utc"):
        return _canonical_utc_minute(value)
    named = {
        "birth_date": _canonical_date,
        "full_name": _canonical_name,
        "amount_minor": _canonical_minor_amount,
        "currency": _canonical_currency,
    }.get(field)
    return named is not None and named(value)


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _canonical_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _canonical_name(value: str) -> bool:
    try:
        return normalize_patient_name(value) == value
    except PatientNameValueError:
        return False


def _canonical_minor_amount(value: str) -> bool:
    return MINOR_AMOUNT.fullmatch(value) is not None


def _canonical_currency(value: str) -> bool:
    return CURRENCY.fullmatch(value) is not None


def _canonical_utc_minute(value: str) -> bool:
    if UTC_MINUTE.fullmatch(value) is None:
        return False
    try:
        _ = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True
