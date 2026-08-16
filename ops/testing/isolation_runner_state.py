"""Expose file-backed runner state transitions for focused recovery tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_claim_records import claim_objects, validate_claim_id
from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_runner_records import (
    begin_discard,
    find_runner_claim,
    finish_discard,
    prepare_runner,
    record_intent,
    runner_creation,
)
from ops.testing.isolation_stack_observation import load_stack_observation

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject


def record_runner_intent(ledger_path: Path, claim_id: str) -> str:
    """Durably authorize one deterministic container before any create call."""
    identifier = validate_claim_id(claim_id)
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = find_runner_claim(claims, identifier)
        creation = record_intent(session, claims, claim)
        return str(creation["intent_sha256"])


def prepare_runner_claim(
    ledger_path: Path,
    claim_id: str,
    observed_path: Path,
) -> None:
    """Record exact created-state identity without authorizing runner use."""
    identifier = validate_claim_id(claim_id)
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = find_runner_claim(claims, identifier)
        observed = load_stack_observation(
            observed_path,
            claim,
            required_state="created",
        )
        prepare_runner(session, claims, claim, observed)


def begin_runner_discard(ledger_path: Path, claim_id: str, reason: str) -> None:
    """Durably record same-boot removal intent before a cleanup side effect."""
    identifier = validate_claim_id(claim_id)
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = find_runner_claim(claims, identifier)
        begin_discard(session, claims, claim, reason, None)


def finish_runner_discard(ledger_path: Path, claim_id: str) -> None:
    """Record removed only after the caller proves the selector union absent."""
    identifier = validate_claim_id(claim_id)
    with locked_open_ledger(ledger_path) as session:
        claim = find_runner_claim(
            claim_objects(session.ledger["claims"]),
            identifier,
        )
        finish_discard(session, claim)


def require_removed_runner_release_ready(claim: JsonObject, claim_root: Path) -> None:
    """Require a completed runner tombstone and absent mutable claim root."""
    if runner_creation(claim).get("state") != "removed":
        _fail("runner creation state is not removed")
    try:
        os.lstat(claim_root)
    except FileNotFoundError:
        return
    _fail("claim root still exists")


def runner_is_prepared_for_activation(claim: JsonObject) -> bool:
    """Return whether one prepared runner may advance without demotion."""
    creation = claim.get("runner_creation")
    return (
        claim.get("kind") == "stack"
        and claim.get("status") == "prepared"
        and isinstance(creation, dict)
        and creation.get("state") == "prepared"
    )


def _fail(message: str) -> Never:
    raise IsolationError(message)
