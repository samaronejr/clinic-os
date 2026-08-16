"""Build and advance the closed stale-boot recovery journal record."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never, cast

import rfc8785

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    raw_sha256,
    utc_now,
)

if TYPE_CHECKING:
    from ops.testing.isolation_stale_actions import StaleActionPlan


@dataclass(frozen=True, slots=True)
class RecoveryDiscovery:
    """Carry only already-authenticated branch-selection facts."""

    has_failure_receipts: bool = False
    terminal_decision: bool = False
    controller_recoveries: tuple[JsonObject, ...] = ()
    f3_recovery_required: bool = False
    f3_journal_path: str | None = None
    f3_initial_journal_sha256: str | None = None
    publisher_claim_id: str | None = None
    publisher_final_wave_journal_path: str | None = None
    publisher_initial_journal_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryPreparation:
    """Bind current-boot facts consumed by prepared-journal construction."""

    current_boot_id: str
    boot_observation: JsonObject
    discovery: RecoveryDiscovery
    timestamp: str


def _fail(message: str) -> Never:
    raise IsolationError(message)


def build_prepared_recovery(
    ledger: JsonObject,
    prior_raw: bytes,
    action_plan: StaleActionPlan,
    preparation: RecoveryPreparation,
) -> JsonObject:
    """Build the immutable prepared journal before any cleanup side effect."""
    current_boot_id = preparation.current_boot_id
    boot_observation = preparation.boot_observation
    discovery = preparation.discovery
    timestamp = preparation.timestamp
    previous_boot = _text(ledger.get("boot_id"), "previous boot ID")
    if previous_boot == current_boot_id:
        _fail("stale recovery requires a changed boot")
    if discovery.terminal_decision:
        _fail("terminal decision requires terminal reconciliation")
    _validate_discovery(discovery)
    if boot_observation.get("boot_id") != current_boot_id:
        _fail("boot observation does not match the current boot")
    if boot_observation.get("observed_at_utc") != timestamp:
        _fail("prepared timestamp differs from boot observation")
    baseline = _object(ledger.get("baseline"), "ledger baseline")
    shared = _object(
        baseline.get("shared_evidence_manifest"),
        "shared evidence manifest",
    )
    lock_identity = _object(ledger.get("lock_identity"), "lock identity")
    reject = (
        discovery.has_failure_receipts
        or discovery.f3_recovery_required
        or bool(discovery.controller_recoveries)
    )
    return {
        "attempt_id": ledger.get("attempt_id"),
        "boot_observation": copy.deepcopy(boot_observation),
        "boot_observation_sha256": _digest(boot_observation),
        "completed_action_ids": [],
        "controller_recoveries": cast(
            "JsonValue",
            [copy.deepcopy(item) for item in discovery.controller_recoveries],
        ),
        "current_boot_id": current_boot_id,
        "f3_final_journal_sha256": None,
        "f3_initial_journal_sha256": discovery.f3_initial_journal_sha256,
        "f3_journal_path": discovery.f3_journal_path,
        "f3_receipt_path": None,
        "f3_receipt_sha256": None,
        "f3_recovery_required": discovery.f3_recovery_required,
        "failure_receipts_sha256": None,
        "lock_identity_sha256": _digest(lock_identity),
        "post_cleanup_ledger_sha256": None,
        "post_update_ledger_sha256": None,
        "previous_boot_id": previous_boot,
        "prior_ledger_sha256": raw_sha256(prior_raw),
        "publisher_claim_id": discovery.publisher_claim_id,
        "publisher_final_journal_sha256": None,
        "publisher_final_wave_journal_path": (
            discovery.publisher_final_wave_journal_path
        ),
        "publisher_initial_journal_sha256": (
            discovery.publisher_initial_journal_sha256
        ),
        "publisher_release_intent_sha256": None,
        "reboot_stable_baseline_sha256": ledger.get("reboot_stable_baseline_sha256"),
        "recovery_goal": "reject" if reject else "resume",
        "resource_actions": cast("JsonValue", copy.deepcopy(action_plan.actions)),
        "resume_execution_host_preflight_path": None,
        "resume_execution_host_preflight_sha256": None,
        "runner_recoveries": cast(
            "JsonValue", copy.deepcopy(action_plan.runner_recoveries)
        ),
        "schema_version": 1,
        "shared_evidence_manifest_sha256": shared.get("sha256"),
        "state": "prepared",
        "updated_at_utc": timestamp,
    }


def complete_stale_action(journal: JsonObject, action_id: str) -> JsonObject:
    """Append only the exact next action and enter resources-absent at the end."""
    state = journal.get("state")
    if state not in {"prepared", "cleaning"}:
        _fail("stale journal is not cleaning resources")
    actions = _objects(journal.get("resource_actions"), "resource actions")
    completed = _strings(journal.get("completed_action_ids"), "completed actions")
    expected_prefix = [str(item.get("action_id")) for item in actions[: len(completed)]]
    if completed != expected_prefix or len(completed) >= len(actions):
        _fail("completed stale actions are not a proper prefix")
    expected = actions[len(completed)].get("action_id")
    if action_id != expected:
        _fail("stale action is not the exact next action")
    runner_matches = [
        item
        for item in _objects(journal.get("runner_recoveries"), "runner recoveries")
        if item.get("action_id") == action_id
    ]
    if runner_matches and (
        len(runner_matches) != 1
        or _object(runner_matches[0].get("runner_creation"), "runner creation").get(
            "state"
        )
        != "removed"
    ):
        _fail("runner recovery must be removed with action completion")
    result = copy.deepcopy(journal)
    result_completed = cast("list[JsonValue]", result["completed_action_ids"])
    result_completed.append(action_id)
    result["state"] = (
        "resources-absent" if len(result_completed) == len(actions) else "cleaning"
    )
    result["updated_at_utc"] = utc_now()
    return result


def mark_stale_resources_absent(journal: JsonObject) -> JsonObject:
    """Advance an authenticated empty action plan to resources-absent."""
    if journal.get("state") != "prepared":
        _fail("empty stale plan is not prepared")
    if _objects(journal.get("resource_actions"), "resource actions"):
        _fail("nonempty stale plan requires physical action completion")
    if _strings(journal.get("completed_action_ids"), "completed actions"):
        _fail("empty stale plan has completed action IDs")
    result = copy.deepcopy(journal)
    result["state"] = "resources-absent"
    result["updated_at_utc"] = utc_now()
    return result


def _validate_discovery(discovery: RecoveryDiscovery) -> None:
    f3_fields = (discovery.f3_journal_path, discovery.f3_initial_journal_sha256)
    if discovery.f3_recovery_required != all(item is not None for item in f3_fields):
        _fail("F3 recovery discovery is incomplete")
    publisher_fields = (
        discovery.publisher_claim_id,
        discovery.publisher_final_wave_journal_path,
        discovery.publisher_initial_journal_sha256,
    )
    if any(item is not None for item in publisher_fields) and not all(
        item is not None for item in publisher_fields
    ):
        _fail("publisher recovery discovery is incomplete")


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


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


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
