"""Reconcile same-boot claims only from authenticated live identities."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Never

from ops.testing.isolation_candidate_contract import contract_for_purpose
from ops.testing.isolation_candidate_reconcile import reconcile_bound_candidate
from ops.testing.isolation_claim_records import (
    claim_objects,
    validate_existing_dependencies,
)
from ops.testing.isolation_common import IsolationError, JsonObject, utc_now
from ops.testing.isolation_filesystem_refresh import (
    classify_filesystem_reservation,
    remove_empty_claim_root,
    require_active_filesystem_unchanged,
)
from ops.testing.isolation_host_inventory import (
    capture_host_inventory as _capture_host_inventory,
)
from ops.testing.isolation_host_inventory import (
    capture_live_host_inventory,
    project_host_inventory,
)
from ops.testing.isolation_host_state import require_known_host_state
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_live_claims import ClaimObserver, observe_live_claim
from ops.testing.isolation_process_observation import validate_process_observation
from ops.testing.isolation_stack_observation import validate_stack_observation

if TYPE_CHECKING:
    from collections.abc import Callable


type ActionKind = Literal["activate", "candidate-release", "release", "refresh"]
capture_host_inventory = _capture_host_inventory


@dataclass(frozen=True, slots=True)
class _Action:
    kind: ActionKind
    claim: JsonObject
    observed: JsonObject | None = None
    remove_root: bool = False


def _fail(message: str) -> Never:
    raise IsolationError(message)


def reconcile_same_boot(
    ledger_path: Path,
    *,
    inventory_reader: Callable[[], JsonObject] | None = None,
    claim_observer: ClaimObserver | None = None,
) -> None:
    """Repair provable same-boot claim states and reject ambiguous resources."""
    observer = observe_live_claim if claim_observer is None else claim_observer
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        if inventory_reader is None:
            live_inventory = capture_live_host_inventory()
            inventory = project_host_inventory(live_inventory)
        else:
            inventory = inventory_reader()
            live_inventory = inventory
        live = _live_observations(claims, live_inventory, observer)
        require_known_host_state(_project_live(session.ledger, live), inventory)
        actions = [
            action
            for claim in claims
            if (
                action := _plan_claim(
                    claim,
                    claims,
                    session.ledger,
                    live.get(str(claim.get("claim_id"))),
                )
            )
            is not None
        ]
        if not actions:
            return
        timestamp = utc_now()
        if any(timestamp <= _last_verified_at(action.claim) for action in actions):
            _fail("reconcile clock did not advance past claim authority")
        for action in actions:
            if action.kind == "release" and action.remove_root:
                claim_root = _claim_root(session.ledger, action.claim)
                remove_empty_claim_root(claim_root)
        for action in actions:
            if action.kind == "candidate-release":
                reconcile_bound_candidate(session, claims, action.claim)
                continue
            _apply_action(action, claims, timestamp)
        session.ledger["last_verified_at_utc"] = timestamp
        session.commit()


def _last_verified_at(claim: JsonObject) -> str:
    value = claim.get("last_verified_at_utc")
    if not isinstance(value, str):
        _fail("claim verification timestamp is not textual")
    return value


def _plan_claim(
    claim: JsonObject,
    claims: list[JsonObject],
    ledger: JsonObject,
    live: JsonObject | None,
) -> _Action | None:
    status = claim.get("status")
    claim_root = _claim_root(ledger, claim)
    if contract_for_purpose(claim.get("purpose")) is not None:
        return _plan_candidate(claim, claims, status)
    if status == "reserved":
        return _plan_reserved(claim, claims, claim_root, live)
    if status != "active":
        return None
    return _plan_active(claim, claims, claim_root, live)


def _plan_candidate(
    claim: JsonObject,
    claims: list[JsonObject],
    status: object,
) -> _Action | None:
    if status != "active" or claim.get("candidate_envelope_binding") is None:
        return None
    _require_no_reverse_dependency(claim, claims)
    return _Action("candidate-release", claim)


def _plan_reserved(
    claim: JsonObject,
    claims: list[JsonObject],
    claim_root: Path,
    live: JsonObject | None,
) -> _Action:
    if claim.get("kind") in {"stack", "process"}:
        if live is None:
            _require_no_reverse_dependency(claim, claims)
            return _Action("release", claim, remove_root=_exists(claim_root))
        _validate_live_observation(claim, live)
        validate_existing_dependencies(claim, claims)
        return _Action("activate", claim, observed=live)
    if claim.get("kind") != "filesystem":
        _fail("reserved claim kind lacks an exact live observer")
    state, observed = classify_filesystem_reservation(claim, claim_root)
    if state == "absent":
        _require_no_reverse_dependency(claim, claims)
        return _Action("release", claim, remove_root=_exists(claim_root))
    validate_existing_dependencies(claim, claims)
    if observed is None:
        _fail("complete filesystem reservation lacks an observation")
    return _Action("activate", claim, observed=observed)


def _plan_active(
    claim: JsonObject,
    claims: list[JsonObject],
    claim_root: Path,
    live: JsonObject | None,
) -> _Action:
    if claim.get("kind") in {"stack", "process"}:
        return _plan_active_live(claim, claims, claim_root, live)
    if claim.get("kind") != "filesystem":
        _fail("active claim kind lacks an exact live refresh")
    if not _exists(claim_root):
        _require_no_reverse_dependency(claim, claims)
        return _Action("release", claim)
    validate_existing_dependencies(claim, claims)
    require_active_filesystem_unchanged(claim, claim_root)
    return _Action("refresh", claim)


def _plan_active_live(
    claim: JsonObject,
    claims: list[JsonObject],
    claim_root: Path,
    live: JsonObject | None,
) -> _Action:
    if live is None:
        _require_no_reverse_dependency(claim, claims)
        return _Action("release", claim, remove_root=_exists(claim_root))
    _validate_live_observation(claim, live)
    if live != claim.get("observed"):
        _fail("active live claim identity drifted")
    validate_existing_dependencies(claim, claims)
    return _Action("refresh", claim)


def _apply_action(action: _Action, claims: list[JsonObject], timestamp: str) -> None:
    claim = action.claim
    if action.kind == "release":
        claims.remove(claim)
        return
    if action.kind == "activate":
        claim["activated_at_utc"] = timestamp
        claim["observed"] = action.observed
        claim["status"] = "active"
    claim["last_verified_at_utc"] = timestamp


def _live_observations(
    claims: list[JsonObject],
    inventory: JsonObject,
    observer: ClaimObserver,
) -> dict[str, JsonObject | None]:
    return {
        str(claim.get("claim_id")): observer(claim, inventory)
        for claim in claims
        if claim.get("kind") in {"stack", "process"}
        and claim.get("status") in {"reserved", "active"}
    }


def _project_live(
    ledger: JsonObject,
    live: dict[str, JsonObject | None],
) -> JsonObject:
    projected = copy.deepcopy(ledger)
    claims = claim_objects(projected["claims"])
    for claim in list(claims):
        identifier = str(claim.get("claim_id"))
        if identifier not in live:
            continue
        observed = live[identifier]
        if observed is None:
            claims.remove(claim)
            continue
        claim["observed"] = observed
        claim["status"] = "active"
    return projected


def _validate_live_observation(claim: JsonObject, observed: JsonObject) -> None:
    if claim.get("kind") == "stack":
        validate_stack_observation(observed, claim)
    else:
        validate_process_observation(observed, claim)


def _require_no_reverse_dependency(
    claim: JsonObject,
    claims: list[JsonObject],
) -> None:
    identifier = claim.get("claim_id")
    for peer in claims:
        dependencies = peer.get("dependency_claim_ids")
        if isinstance(dependencies, list) and identifier in dependencies:
            _fail("claim has a reverse dependency")


def _claim_root(ledger: JsonObject, claim: JsonObject) -> Path:
    return Path(str(ledger["attempt_root"])) / str(claim["root_relative_path"])


def _exists(path: Path) -> bool:
    try:
        _ = os.lstat(path)
    except FileNotFoundError:
        return False
    return True
