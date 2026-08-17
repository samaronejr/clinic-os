from __future__ import annotations

from typing import TYPE_CHECKING, Never

from django.core.exceptions import ImproperlyConfigured
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_sha256,
    raw_sha256,
    utc_now,
)
from ops.testing.isolation_ledger_store import locked_open_ledger

from .browser_authority_claims import validate_browser_claims
from .browser_authority_root import text, validate_authority_root

if TYPE_CHECKING:
    from pathlib import Path


def validate_browser_ledger(
    ledger: JsonObject,
    expected: JsonObject,
    phase: str,
) -> None:
    try:
        attempt_root = validate_authority_root(ledger, expected, phase)
        validate_browser_claims(ledger, expected, phase, attempt_root)
    except (IsolationError, KeyError, OSError, TypeError, ValueError):
        _fail()


def attest_browser_authority(
    ledger_path: Path,
    expected: JsonObject,
    phase: str,
) -> JsonObject:
    try:
        with locked_open_ledger(ledger_path) as session:
            validate_browser_ledger(session.ledger, expected, phase)
            process = _process_claim(
                session.ledger, text(expected.get("process_claim_id"))
            )
            return {
                "attempt_id": text(expected.get("attempt_id")),
                "claim_id": text(process.get("claim_id")),
                "claim_status": text(process.get("status")),
                "ledger_sha256": raw_sha256(session.original_raw),
                "observation_sha256": canonical_sha256(process.get("observed")),
                "schema_version": 1,
                "sequence": 1,
                "verified_at_utc": utc_now(),
            }
    except (
        ImproperlyConfigured,
        IsolationError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        _fail()


def _process_claim(ledger: JsonObject, claim_id: str) -> JsonObject:
    claims = ledger.get("claims")
    if not isinstance(claims, list):
        raise TypeError
    matches: list[JsonObject] = []
    for claim in claims:
        if not isinstance(claim, dict):
            raise TypeError
        if claim.get("claim_id") == claim_id:
            matches.append(claim)
    if len(matches) != 1:
        raise ValueError
    return matches[0]


def _fail() -> Never:
    message = "browser authority violates the closed ledger contract"
    raise ImproperlyConfigured(message)
