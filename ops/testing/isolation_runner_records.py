"""Apply runner write-ahead states inside one retained ledger session."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Final, Never, cast

import rfc8785

from ops.testing.isolation_claim_records import validate_existing_dependencies
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    raw_sha256,
    utc_now,
)
from ops.testing.isolation_runner_create import runner_create_argv_sha
from ops.testing.isolation_runner_dependencies import (
    require_no_runner_reverse_dependency,
)
from ops.testing.isolation_stack_observation import validate_stack_observation

if TYPE_CHECKING:
    from ops.testing.isolation_ledger_store import LedgerSession

DISCARD_REASONS: Final = frozenset({"owner-cleanup", "owner-lost", "unsafe-state"})
CONTAINER_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


def find_runner_claim(claims: list[JsonObject], claim_id: str) -> JsonObject:
    """Return the unique runner claim named by canonical UUID text."""
    matches = [claim for claim in claims if claim.get("claim_id") == claim_id]
    if len(matches) != 1:
        _fail("runner claim identity is missing or duplicated")
    runner_service(matches[0])
    return matches[0]


def runner_service(claim: JsonObject) -> JsonObject:
    """Return the sole prepared-attestation service of a runner claim."""
    if claim.get("kind") != "stack":
        _fail("runner claim is not a stack")
    desired = _object(claim.get("desired"), "runner desired")
    services = _objects(desired.get("services"), "runner services")
    matches = [
        item
        for item in services
        if item.get("start_policy") == "prepared-attest-before-use"
    ]
    if len(services) != 1 or len(matches) != 1:
        _fail("runner claim requires one prepared-attestation service")
    return matches[0]


def runner_creation(claim: JsonObject) -> JsonObject:
    """Return the nonnull closed runner creation record."""
    creation = claim.get("runner_creation")
    if not isinstance(creation, dict):
        _fail("runner creation state is absent")
    return creation


def record_intent(
    session: LedgerSession,
    claims: list[JsonObject],
    claim: JsonObject,
) -> JsonObject:
    """Commit deterministic create authority before the first Docker call."""
    service = runner_service(claim)
    if claim.get("status") != "reserved" or claim.get("runner_creation") is not None:
        _fail("runner claim is not reserved without creation state")
    validate_existing_dependencies(claim, claims)
    timestamp = utc_now()
    _require_later(timestamp, claim.get("reserved_at_utc"), "runner intent")
    identifier = cast("str", claim["claim_id"])
    intent_id = str(uuid.uuid4())
    container_name = f"clinic-phase1a-runner-{identifier}"
    desired_sha = raw_sha256(rfc8785.dumps(service))
    create_sha = runner_create_argv_sha(
        session.ledger,
        claim,
        service,
        container_name,
    )
    core: JsonObject = {
        "attempt_id": session.ledger["attempt_id"],
        "claim_id": identifier,
        "container_name": container_name,
        "create_argv_sha256": create_sha,
        "creation_boot_id": session.ledger["boot_id"],
        "desired_service_sha256": desired_sha,
        "intent_id": intent_id,
        "schema_version": 1,
        "service_name": service["name"],
    }
    creation: JsonObject = {
        "container_id": None,
        "container_name": container_name,
        "create_argv_sha256": create_sha,
        "creation_boot_id": session.ledger["boot_id"],
        "desired_service_sha256": desired_sha,
        "intent_at_utc": timestamp,
        "intent_id": intent_id,
        "intent_sha256": raw_sha256(rfc8785.dumps(core)),
        "remove_intent_at_utc": None,
        "remove_reason": None,
        "removed_at_utc": None,
        "schema_version": 1,
        "service_name": service["name"],
        "state": "intent",
    }
    claim["runner_creation"] = creation
    _advance(session, claim, timestamp)
    return creation


def prepare_runner(
    session: LedgerSession,
    claims: list[JsonObject],
    claim: JsonObject,
    observed: JsonObject,
) -> str:
    """Commit one exact never-started observation as prepared authority."""
    creation = runner_creation(claim)
    if claim.get("status") != "reserved" or creation.get("state") != "intent":
        _fail("runner claim is not at creation intent")
    validate_existing_dependencies(claim, claims)
    validate_stack_observation(observed, claim, required_state="created")
    container_ids = observed.get("container_ids")
    if not isinstance(container_ids, list) or len(container_ids) != 1:
        _fail("prepared runner requires one created container ID")
    container_id = container_ids[0]
    if (
        not isinstance(container_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
    ):
        _fail("prepared runner container ID is invalid")
    timestamp = utc_now()
    _require_later(timestamp, creation.get("intent_at_utc"), "runner prepare")
    creation["container_id"] = container_id
    creation["state"] = "prepared"
    claim["observed"] = observed
    claim["prepared_at_utc"] = timestamp
    claim["status"] = "prepared"
    _advance(session, claim, timestamp)
    return container_id


def begin_discard(
    session: LedgerSession,
    claims: list[JsonObject],
    claim: JsonObject,
    reason: str,
    container_id: str | None,
) -> JsonObject:
    """Commit remove-intent and an optional intent-only captured ID."""
    if reason not in DISCARD_REASONS:
        _fail("runner discard reason is invalid")
    creation = runner_creation(claim)
    if creation.get("creation_boot_id") != session.ledger.get("boot_id"):
        _fail("runner creation belongs to a different boot")
    state = creation.get("state")
    if state in {"remove-intent", "removed"}:
        if creation.get("remove_reason") != reason:
            _fail("runner discard reason changed during replay")
        capture_container_id(session, claim, container_id)
        return creation
    if (claim.get("status"), state) not in {
        ("reserved", "intent"),
        ("prepared", "prepared"),
        ("active", "prepared"),
    }:
        _fail("runner state cannot begin discard")
    require_no_runner_reverse_dependency(claims, claim)
    validate_existing_dependencies(claim, claims)
    capture_container_id(session, claim, container_id, commit=False)
    timestamp = utc_now()
    _require_later(timestamp, claim.get("last_verified_at_utc"), "runner discard")
    creation["remove_intent_at_utc"] = timestamp
    creation["remove_reason"] = reason
    creation["state"] = "remove-intent"
    _advance(session, claim, timestamp)
    return creation


def capture_container_id(
    session: LedgerSession,
    claim: JsonObject,
    container_id: str | None,
    *,
    commit: bool = True,
) -> None:
    """Durably bind a sole intent-union ID before its physical deletion."""
    if container_id is None:
        return
    if CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
        _fail("runner container ID is invalid")
    creation = runner_creation(claim)
    existing = creation.get("container_id")
    if existing not in {None, container_id}:
        _fail("runner container ID changed during cleanup")
    if existing == container_id:
        return
    creation["container_id"] = container_id
    if commit:
        session.commit()


def finish_discard(session: LedgerSession, claim: JsonObject) -> None:
    """Commit removed only after the selector union is freshly absent."""
    creation = runner_creation(claim)
    if creation.get("state") == "removed":
        return
    if creation.get("state") != "remove-intent":
        _fail("runner is not at removal intent")
    timestamp = utc_now()
    _require_later(timestamp, creation.get("remove_intent_at_utc"), "runner removal")
    creation["removed_at_utc"] = timestamp
    creation["state"] = "removed"
    _advance(session, claim, timestamp)


def _advance(session: LedgerSession, claim: JsonObject, timestamp: str) -> None:
    claim["last_verified_at_utc"] = timestamp
    session.ledger["last_verified_at_utc"] = timestamp
    session.commit()


def _require_later(current: str, previous: JsonValue, context: str) -> None:
    if not isinstance(previous, str) or current <= previous:
        _fail(f"{context} timestamp is not strictly ordered")


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _fail(message: str) -> Never:
    raise IsolationError(message)
