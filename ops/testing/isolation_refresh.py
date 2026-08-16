"""Refresh active isolation claims against same-boot live observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_candidate_contract import contract_for_purpose
from ops.testing.isolation_claim_records import (
    claim_objects,
    validate_claim_id,
    validate_existing_dependencies,
)
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue, utc_now
from ops.testing.isolation_filesystem_refresh import require_active_filesystem_unchanged
from ops.testing.isolation_host_inventory import (
    capture_host_inventory as _capture_host_inventory,
)
from ops.testing.isolation_host_inventory import (
    capture_live_host_inventory,
    project_host_inventory,
)
from ops.testing.isolation_host_state import require_known_host_state
from ops.testing.isolation_ledger_store import LedgerSession, locked_open_ledger
from ops.testing.isolation_live_claims import ClaimObserver, observe_live_claim
from ops.testing.isolation_process_observation import validate_process_observation
from ops.testing.isolation_stack_observation import validate_stack_observation

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_REFRESH_AGE: Final = timedelta(seconds=60)
capture_host_inventory = _capture_host_inventory


def _fail(message: str) -> Never:
    raise IsolationError(message)


def verify_claim(
    ledger_path: Path,
    claim_id: str,
    *,
    refresh: bool,
    inventory_reader: Callable[[], JsonObject] | None = None,
    claim_observer: ClaimObserver | None = None,
) -> None:
    """Verify one active claim and its dependency DAG without widening authority."""
    identifier = validate_claim_id(claim_id)
    observer = observe_live_claim if claim_observer is None else claim_observer
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        ordered = _dependency_order(claims, identifier)
        if inventory_reader is None:
            live_inventory = capture_live_host_inventory()
            inventory = project_host_inventory(live_inventory)
        else:
            inventory = inventory_reader()
            live_inventory = inventory
        require_known_host_state(session.ledger, inventory)
        if refresh:
            _refresh_claims(session, claims, ordered, live_inventory, observer)
            return
        for claim in ordered:
            validate_existing_dependencies(claim, claims)
            _require_fresh(claim.get("last_verified_at_utc"))


def _refresh_claims(
    session: LedgerSession,
    claims: list[JsonObject],
    ordered: list[JsonObject],
    inventory: JsonObject,
    observer: ClaimObserver,
) -> None:
    for claim in ordered:
        validate_existing_dependencies(claim, claims)
        _require_refreshable_active_claim(claim, session.ledger, inventory, observer)
    timestamp = utc_now()
    for claim in ordered:
        previous = claim.get("last_verified_at_utc")
        if not isinstance(previous, str) or timestamp <= previous:
            _fail("refresh clock did not advance past claim authority")
        claim["last_verified_at_utc"] = timestamp
    session.ledger["last_verified_at_utc"] = timestamp
    session.commit()


def _require_refreshable_active_claim(
    claim: JsonObject,
    ledger: JsonObject,
    inventory: JsonObject,
    observer: ClaimObserver,
) -> None:
    if claim.get("status") != "active":
        _fail("verified claim dependency DAG is not active")
    kind = claim.get("kind")
    claim_root = Path(str(ledger["attempt_root"])) / str(claim["root_relative_path"])
    if kind == "filesystem" and contract_for_purpose(claim.get("purpose")) is None:
        require_active_filesystem_unchanged(claim, claim_root)
        return
    if kind in {"stack", "process"}:
        observed = observer(claim, inventory)
        if observed is None:
            _fail("active live claim disappeared during refresh")
        if kind == "stack":
            validate_stack_observation(observed, claim)
        else:
            validate_process_observation(observed, claim)
        if observed != claim.get("observed"):
            _fail("active live claim identity drifted")
        return
    _fail("active claim kind lacks a same-boot live refresh implementation")


def _dependency_order(
    claims: list[JsonObject],
    claim_id: str,
) -> list[JsonObject]:
    by_id: dict[str, JsonObject] = {}
    for claim in claims:
        identifier = claim.get("claim_id")
        if not isinstance(identifier, str) or identifier in by_id:
            _fail("claim dependency authority is missing or duplicated")
        by_id[identifier] = claim
    if claim_id not in by_id:
        _fail("verified claim is missing")
    ordered: list[JsonObject] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            _fail("claim dependency graph contains a cycle")
        if identifier in visited:
            return
        claim = by_id.get(identifier)
        if claim is None:
            _fail("claim dependency is missing")
        visiting.add(identifier)
        dependencies = _strings(
            claim.get("dependency_claim_ids"),
            "claim dependencies",
        )
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)
        ordered.append(claim)

    visit(claim_id)
    return ordered


def _require_fresh(value: JsonValue) -> None:
    if not isinstance(value, str):
        _fail("claim refresh timestamp is absent")
    try:
        observed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        current = datetime.strptime(
            utc_now(),
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=UTC)
    except ValueError as error:
        message = "claim refresh timestamp is malformed"
        raise IsolationError(message) from error
    age = current - observed
    if age < timedelta(0) or age > MAX_REFRESH_AGE:
        _fail("claim authority is not fresh")


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{context} are not a string array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail(f"{context} are not a string array")
        result.append(item)
    return result
