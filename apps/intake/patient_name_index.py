"""Versioned normalization for exact name HMACs; never a plaintext shadow."""

import unicodedata

from apps.core.idempotency import normalize_patient_name
from apps.tenancy.envelope import blind_indexes

PURPOSE = "intake.patient.full_name.exact.v1"


def normalized_name_bytes(value: str) -> bytes:
    """Fold case, accents and spacing consistently on writes and exact lookups."""
    name = unicodedata.normalize("NFKD", normalize_patient_name(value))
    folded = "".join(char for char in name if not unicodedata.combining(char))
    return " ".join(folded.casefold().split()).encode("utf-8")


def name_indexes(value: str) -> tuple[bytes, ...]:
    """Compute lookup digests without reading any stored patient plaintext."""
    return blind_indexes(purpose=PURPOSE, plaintext=normalized_name_bytes(value))
