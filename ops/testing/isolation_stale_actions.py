"""Derive the immutable reverse-topological changed-boot action plan."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

import rfc8785

from ops.testing.isolation_candidate_contract import contract_for_purpose
from ops.testing.isolation_candidate_stale_actions import build_candidate_stale_actions
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_stale_action_identities import (
    build_claim_action_identities,
)


@dataclass(frozen=True, slots=True)
class StaleActionPlan:
    """Keep public summaries beside their full authenticated identity objects."""

    actions: list[JsonObject]
    identities: list[JsonObject]
    runner_recoveries: list[JsonObject]


def _fail(message: str) -> Never:
    raise IsolationError(message)


def build_stale_action_plan(
    ledger: JsonObject,
    *,
    remove_publisher_staging: bool = False,
) -> StaleActionPlan:
    """Build one deterministic changed-boot physical cleanup plan."""
    attempt_id = _text(ledger.get("attempt_id"), "attempt ID")
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    claims = _objects(ledger.get("claims"), "claims")
    actions: list[JsonObject] = []
    identities: list[JsonObject] = []
    recoveries: list[JsonObject] = []
    for claim in _reverse_topological(claims):
        if claim.get("purpose") == "final-terminal-publisher":
            if remove_publisher_staging:
                claim_identities, _claim_recoveries = build_claim_action_identities(
                    attempt_id,
                    attempt_root,
                    claim,
                )
                for identity in claim_identities:
                    _append(actions, identities, identity)
            continue
        candidate = contract_for_purpose(claim.get("purpose"))
        if candidate is not None:
            summaries, candidate_identities = build_candidate_stale_actions(
                ledger, claim
            )
            actions.extend(summaries)
            identities.extend(candidate_identities)
            continue
        claim_identities, claim_recoveries = build_claim_action_identities(
            attempt_id,
            attempt_root,
            claim,
        )
        for identity in claim_identities:
            _append(actions, identities, identity)
        recoveries.extend(claim_recoveries)
    action_ids = [item.get("action_id") for item in actions]
    if len(action_ids) != len(set(action_ids)):
        _fail("stale action IDs are duplicated")
    recoveries.sort(key=lambda item: (str(item["action_id"]), str(item["claim_id"])))
    return StaleActionPlan(actions, identities, recoveries)


def _append(
    actions: list[JsonObject],
    identities: list[JsonObject],
    identity: JsonObject,
) -> None:
    identities.append(identity)
    actions.append(
        {
            "action_id": identity["action_id"],
            "claim_id": identity["claim_id"],
            "identity_sha256": _digest(identity),
            "operation": identity["operation"],
            "resource_kind": identity["resource_kind"],
        }
    )


def _reverse_topological(claims: list[JsonObject]) -> list[JsonObject]:
    by_id = {_text(item.get("claim_id"), "claim ID"): item for item in claims}
    if len(by_id) != len(claims):
        _fail("stale claims contain duplicate identities")
    ordered: list[JsonObject] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_id: str) -> None:
        if claim_id in visiting:
            _fail("stale claim dependency cycle")
        if claim_id in visited:
            return
        claim = by_id.get(claim_id)
        if claim is None:
            _fail("stale claim dependency is missing")
        visiting.add(claim_id)
        dependencies = _strings(claim.get("dependency_claim_ids"), "dependencies")
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(claim_id)
        visited.add(claim_id)
        ordered.append(claim)

    for identifier in sorted(by_id):
        visit(identifier)
    return list(reversed(ordered))


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
