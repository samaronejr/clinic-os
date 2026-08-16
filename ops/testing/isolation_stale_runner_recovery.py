"""Advance copied runner tombstones around changed-boot Docker removal."""

from __future__ import annotations

import copy
from typing import Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue, utc_now
from ops.testing.isolation_stale_records import complete_stale_action

DISCARD_REASONS = frozenset({"owner-cleanup", "owner-lost", "unsafe-state"})


def has_runner_recovery(journal: JsonObject, action_id: str) -> bool:
    """Return whether an action owns one copied runner-creation tombstone."""
    return any(
        item.get("action_id") == action_id
        for item in _objects(journal.get("runner_recoveries"), "runner recoveries")
    )


def begin_stale_runner_removal(journal: JsonObject, action_id: str) -> JsonObject:
    """Fsync-ready transition the copied runner to remove-intent."""
    result = copy.deepcopy(journal)
    recovery = _recovery(result, action_id)
    creation = _object(recovery.get("runner_creation"), "runner creation")
    state = creation.get("state")
    if state == "remove-intent":
        _require_remove_intent(creation)
        return result
    legal = (recovery.get("origin_status"), state) in {
        ("reserved", "intent"),
        ("prepared", "prepared"),
        ("active", "prepared"),
    }
    if not legal:
        _fail("runner recovery origin cannot begin removal")
    timestamp = utc_now()
    _require_later(timestamp, creation.get("intent_at_utc"), "runner removal intent")
    creation["remove_intent_at_utc"] = timestamp
    creation["remove_reason"] = "owner-lost"
    creation["removed_at_utc"] = None
    creation["state"] = "remove-intent"
    result["updated_at_utc"] = timestamp
    return result


def complete_stale_runner_removal(
    journal: JsonObject,
    action_id: str,
) -> JsonObject:
    """Atomically mark the copied runner removed with outer action completion."""
    result = copy.deepcopy(journal)
    recovery = _recovery(result, action_id)
    creation = _object(recovery.get("runner_creation"), "runner creation")
    _require_remove_intent(creation)
    timestamp = utc_now()
    _require_later(
        timestamp,
        creation.get("remove_intent_at_utc"),
        "runner removal completion",
    )
    creation["removed_at_utc"] = timestamp
    creation["state"] = "removed"
    result["updated_at_utc"] = timestamp
    return complete_stale_action(result, action_id)


def validate_runner_recovery_prefixes(journal: JsonObject) -> None:
    """Require copied tombstones to agree with the outer completed prefix."""
    completed = _strings(journal.get("completed_action_ids"), "completed actions")
    actions = _objects(journal.get("resource_actions"), "resource actions")
    next_id = (
        actions[len(completed)].get("action_id")
        if len(completed) < len(actions)
        else None
    )
    for recovery in _objects(journal.get("runner_recoveries"), "runner recoveries"):
        action_id = recovery.get("action_id")
        creation = _object(recovery.get("runner_creation"), "runner creation")
        state = creation.get("state")
        if action_id in completed:
            if state != "removed":
                _fail("completed runner action lacks a removed tombstone")
        elif state == "removed":
            _fail("removed runner tombstone lacks atomic action completion")
        elif state == "remove-intent":
            _require_remove_intent(creation)
            if action_id != next_id:
                _fail("runner remove-intent is not the exact next action")


def _recovery(journal: JsonObject, action_id: str) -> JsonObject:
    matches = [
        item
        for item in _objects(journal.get("runner_recoveries"), "runner recoveries")
        if item.get("action_id") == action_id
    ]
    if len(matches) != 1:
        _fail("runner recovery action is missing or duplicated")
    return matches[0]


def _require_remove_intent(creation: JsonObject) -> None:
    if (
        creation.get("state") != "remove-intent"
        or creation.get("remove_reason") not in DISCARD_REASONS
        or not isinstance(creation.get("remove_intent_at_utc"), str)
        or creation.get("removed_at_utc") is not None
    ):
        _fail("runner recovery remove-intent fields are invalid")


def _require_later(current: str, prior: JsonValue, context: str) -> None:
    if not isinstance(prior, str) or current <= prior:
        _fail(f"{context} timestamp is not strictly ordered")


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


def _fail(message: str) -> Never:
    raise IsolationError(message)
