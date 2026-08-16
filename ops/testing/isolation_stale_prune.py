"""Apply the write-ahead stale-boot claim-prune boundary."""

from __future__ import annotations

import copy
from typing import Never, cast

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    raw_sha256,
    utc_now,
)


def build_pruned_ledger(
    ledger: JsonObject,
    journal: JsonObject,
) -> tuple[JsonObject, bytes]:
    """Remove nonpublisher claims without changing prior boot/proof fields."""
    if journal.get("state") not in {"resources-absent", "claims-prune-intent"}:
        _fail("claims cannot prune before resources are absent")
    if raw_sha256(canonical_bytes(ledger)) != journal.get("prior_ledger_sha256"):
        _fail("prior ledger changed before claims prune")
    actions = _objects(journal.get("resource_actions"), "resource actions")
    completed = _strings(journal.get("completed_action_ids"), "completed actions")
    if completed != [str(item.get("action_id")) for item in actions]:
        _fail("resource action plan is incomplete")
    recoveries = _objects(journal.get("runner_recoveries"), "runner recoveries")
    if any(
        _object(item.get("runner_creation"), "runner creation").get("state")
        != "removed"
        for item in recoveries
    ):
        _fail("runner recovery is not removed")
    result = copy.deepcopy(ledger)
    claims = _objects(result.get("claims"), "ledger claims")
    publishers = [
        claim for claim in claims if claim.get("purpose") == "final-terminal-publisher"
    ]
    if len(publishers) > 1:
        _fail("multiple persistent publishers cannot survive claims prune")
    result["claims"] = cast("JsonValue", publishers)
    raw = canonical_bytes(result)
    bound = journal.get("post_cleanup_ledger_sha256")
    if bound is not None and bound != raw_sha256(raw):
        _fail("reconstructed claims prune differs from write-ahead authority")
    return result, raw


def bind_claims_prune_intent(
    journal: JsonObject,
    pruned_raw: bytes,
) -> JsonObject:
    """Bind prospective pruned bytes before the sole canonical replacement."""
    if journal.get("state") != "resources-absent":
        _fail("claims prune intent requires resources-absent")
    if journal.get("post_cleanup_ledger_sha256") is not None:
        _fail("claims prune hash is already bound")
    result = copy.deepcopy(journal)
    result["post_cleanup_ledger_sha256"] = raw_sha256(pruned_raw)
    result["state"] = "claims-prune-intent"
    result["updated_at_utc"] = utc_now()
    return result


def mark_claims_pruned(journal: JsonObject, ledger_raw: bytes) -> JsonObject:
    """Advance only after the canonical ledger equals the bound prune hash."""
    if journal.get("state") != "claims-prune-intent":
        _fail("claims are not at prune intent")
    if raw_sha256(ledger_raw) != journal.get("post_cleanup_ledger_sha256"):
        _fail("pruned ledger does not match write-ahead authority")
    result = copy.deepcopy(journal)
    result["state"] = "claims-pruned"
    result["updated_at_utc"] = utc_now()
    return result


def _fail(message: str) -> Never:
    raise IsolationError(message)


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)
