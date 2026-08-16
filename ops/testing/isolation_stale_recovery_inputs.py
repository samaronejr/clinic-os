"""Authenticate current-boot inputs and durable stale-recovery replay fields."""

from __future__ import annotations

import copy
from typing import Never, cast

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    raw_sha256,
)
from ops.testing.isolation_snapshot_records import reboot_stable_baseline_sha256
from ops.testing.isolation_stale_records import RecoveryDiscovery


def require_reboot_stable(ledger: JsonObject, inventory: JsonObject) -> None:
    """Require live ambient inventory to match the creation baseline projection."""
    baseline = _object(ledger.get("baseline"), "baseline")
    actual = reboot_stable_baseline_sha256(baseline, inventory)
    if actual != ledger.get("reboot_stable_baseline_sha256"):
        _fail("current ambient inventory differs from the reboot-stable baseline")


def boot_observation(
    boot_id: str,
    timestamp: str,
    inventory: JsonObject,
) -> JsonObject:
    """Freeze the current inventory into the closed boot-observation shape."""
    return {
        "boot_id": boot_id,
        "containers": copy.deepcopy(inventory.get("containers")),
        "listeners": copy.deepcopy(inventory.get("listeners")),
        "networks": copy.deepcopy(inventory.get("networks")),
        "observed_at_utc": timestamp,
        "volumes": copy.deepcopy(inventory.get("volumes")),
    }


def replay_discovery(journal: JsonObject) -> RecoveryDiscovery:
    """Reconstruct only immutable discovery inputs from an existing journal."""
    controllers = tuple(_objects(journal.get("controller_recoveries"), "controllers"))
    return RecoveryDiscovery(
        has_failure_receipts=journal.get("recovery_goal") == "reject",
        controller_recoveries=controllers,
        f3_recovery_required=journal.get("f3_recovery_required") is True,
        f3_journal_path=_optional_text(journal.get("f3_journal_path")),
        f3_initial_journal_sha256=_optional_text(
            journal.get("f3_initial_journal_sha256")
        ),
        publisher_claim_id=_optional_text(journal.get("publisher_claim_id")),
        publisher_final_wave_journal_path=_optional_text(
            journal.get("publisher_final_wave_journal_path")
        ),
        publisher_initial_journal_sha256=_optional_text(
            journal.get("publisher_initial_journal_sha256")
        ),
    )


def validate_post_prune_prefix(
    ledger: JsonObject,
    ledger_raw: bytes,
    journal: JsonObject,
    current_boot: str,
) -> None:
    """Authenticate the sole journal states allowed beside pruned ledger bytes."""
    state = journal.get("state")
    if state not in {
        "claims-prune-intent",
        "claims-pruned",
        "resume-proof-published",
        "receipts-finalizing",
        "receipts-finalized",
        "publisher-release-intent",
        "boot-updated",
        "publisher-released",
        "complete",
    }:
        _fail("stale ledger changed before the write-ahead prune state")
    current_hash = raw_sha256(ledger_raw)
    allowed = {journal.get("post_cleanup_ledger_sha256")}
    if state in {
        "resume-proof-published",
        "receipts-finalized",
        "publisher-release-intent",
        "boot-updated",
        "publisher-released",
        "complete",
    }:
        allowed.add(journal.get("post_update_ledger_sha256"))
    if current_hash not in allowed:
        _fail("post-prune ledger hash differs from stale journal authority")
    if journal.get("attempt_id") != ledger.get("attempt_id"):
        _fail("post-prune journal attempt differs from ledger")
    if journal.get("current_boot_id") != current_boot:
        _fail("post-prune journal belongs to a different recovery boot")
    if current_hash == journal.get("post_update_ledger_sha256"):
        if ledger.get("boot_id") != current_boot:
            _fail("post-update ledger did not bind the current boot")
    elif ledger.get("boot_id") != journal.get("previous_boot_id"):
        _fail("post-cleanup ledger changed its prior boot prematurely")


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _optional_text(value: JsonValue) -> str | None:
    if value is not None and not isinstance(value, str):
        _fail("optional journal field must be a string or null")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
