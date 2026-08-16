"""Discover and authenticate a final-wave publisher journal binding."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never, cast

import rfc8785

from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    stat_identity,
)
from ops.testing.isolation_terminal_publisher_authorizations import (
    validate_terminal_publisher_claim,
)
from ops.testing.isolation_terminal_publisher_contract import (
    JOURNAL_KEYS,
    RELEASE_KEYS,
    validate_final_wave_journal,
)

__all__ = ["JOURNAL_KEYS", "RELEASE_KEYS"]

JOURNAL_NAME: Final = "final-wave-state.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublisherBinding:
    """Bind the retained claim to its exact initial final-wave journal."""

    claim_id: str
    journal_path: str
    initial_journal_sha256: str


def discover_terminal_publisher(ledger: JsonObject) -> PublisherBinding | None:
    """Find the sole retained publisher and authenticate its initial journal."""
    claims = _objects(ledger.get("claims"), "ledger claims")
    publishers = [
        item for item in claims if item.get("purpose") == "final-terminal-publisher"
    ]
    if not publishers:
        return None
    if len(publishers) != 1:
        _fail("multiple terminal publishers are bound to the stale ledger")
    claim = publishers[0]
    attempt_root = Path(_absolute_text(ledger.get("attempt_root"), "attempt root"))
    path = attempt_root / JOURNAL_NAME
    journal, raw = load_terminal_publisher_journal(path)
    validate_initial_publisher_journal(ledger, claim, journal)
    claim_id = _text(claim.get("claim_id"), "publisher claim ID")
    return PublisherBinding(claim_id, str(path), raw_sha256(raw))


def load_terminal_publisher_journal(path: Path) -> tuple[JsonObject, bytes]:
    """Load the exact private canonical final-wave publisher journal."""
    regular_identity(path, mode=MODE_PRIVATE)
    journal, raw = load_json(path)
    validate_final_wave_journal(journal)
    return journal, raw


def validate_initial_publisher_journal(
    ledger: JsonObject,
    claim: JsonObject,
    journal: JsonObject,
) -> str:
    """Require reservation identity, claim bytes, and authorization state bound."""
    _validate_root(journal, ledger)
    claim_id = _text(claim.get("claim_id"), "publisher claim ID")
    if journal.get("terminal_publisher_claim_id") != claim_id:
        _fail("final-wave journal names another publisher claim")
    if journal.get("terminal_publisher_state") != claim.get("status"):
        _fail("final-wave publisher state differs from its retained claim")
    if any(journal.get(key) is not None for key in RELEASE_KEYS):
        _fail("initial final-wave publisher release fields are already bound")
    spec = {
        "claim_id": claim["claim_id"],
        "dependency_claim_ids": claim["dependency_claim_ids"],
        "desired": claim["desired"],
        "kind": claim["kind"],
        "purpose": claim["purpose"],
    }
    if raw_sha256(rfc8785.dumps(spec)) != journal.get(
        "terminal_publisher_reservation_spec_sha256"
    ):
        _fail("terminal publisher reservation spec hash drifted")
    for key in (
        "terminal_publisher_pre_reservation_ledger_sha256",
        "terminal_publisher_post_reservation_ledger_sha256",
    ):
        _sha256(journal.get(key), key)
    terminal_root = Path(_absolute_text(journal.get("control_root"), "control root"))
    terminal_root /= "terminal"
    return validate_terminal_publisher_claim(claim, terminal_root)


def _validate_root(journal: JsonObject, ledger: JsonObject) -> None:
    validate_final_wave_journal(journal)
    if journal.get("attempt_id") != ledger.get("attempt_id") or journal.get(
        "sha"
    ) != ledger.get("foundation_sha"):
        _fail("final-wave publisher journal belongs to another attempt or SHA")
    if journal.get("phase") not in {
        "initializing",
        "inputs-frozen",
        "lanes-complete",
        "pre-f4-frozen",
        "f4-rejected",
        "f4-created",
        "final-frozen",
        "rejected",
    }:
        _fail("final-wave phase is unknown")
    attempt_root = Path(_absolute_text(ledger.get("attempt_root"), "attempt root"))
    expected_control = attempt_root.parents[1] / "clinic-os-phase1a-final"
    if journal.get("control_root") != str(expected_control):
        _fail("final-wave control root escaped the authority evidence root")
    _require_named_identity(expected_control, journal.get("control_root_identity"))
    _require_named_identity(
        expected_control / "final.lock", journal.get("lock_identity")
    )


def _require_named_identity(path: Path, value: JsonValue) -> None:
    expected = _object(value, "final-wave named identity")
    actual = stat_identity(path)
    actual.pop("link_count")
    if expected != actual:
        _fail("final-wave control or lock identity drifted")


def _sha256(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        _fail(f"{context} is not a SHA-256")
    return value


def raw_initial_journal_hash(journal: JsonObject) -> str:
    """Hash one reconstructed initial journal using canonical file bytes."""
    return raw_sha256(canonical_bytes(journal))


def _absolute_text(value: JsonValue, context: str) -> str:
    text = _text(value, context)
    if not Path(text).is_absolute():
        _fail(f"{context} is not absolute")
    return text


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
