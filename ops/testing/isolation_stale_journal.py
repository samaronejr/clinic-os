"""Create, authenticate, and replace the stale-boot outer journal."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Never, cast

from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    regular_identity,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_stale_runner_recovery import (
    validate_runner_recovery_prefixes,
)

RECOVERY_NAME: Final = "stale-boot-recovery.json"
MUTABLE_KEYS: Final = frozenset(
    {
        "completed_action_ids",
        "controller_recoveries",
        "f3_final_journal_sha256",
        "f3_receipt_path",
        "f3_receipt_sha256",
        "failure_receipts_sha256",
        "post_cleanup_ledger_sha256",
        "post_update_ledger_sha256",
        "publisher_final_journal_sha256",
        "publisher_release_intent_sha256",
        "resume_execution_host_preflight_path",
        "resume_execution_host_preflight_sha256",
        "runner_recoveries",
        "state",
        "updated_at_utc",
    }
)


def recovery_journal_path(ledger: JsonObject) -> Path:
    """Derive the sole outer-journal path from the authenticated attempt root."""
    attempt_root = ledger.get("attempt_root")
    if not isinstance(attempt_root, str):
        _fail("ledger attempt root is not a string")
    root = Path(attempt_root)
    if not root.is_absolute():
        _fail("ledger attempt root is not absolute")
    return root / RECOVERY_NAME


def load_recovery_journal(path: Path) -> JsonObject | None:
    """Load one private canonical journal or report its exact absence."""
    try:
        regular_identity(path, mode=MODE_PRIVATE)
    except FileNotFoundError:
        return None
    journal, _ = load_json(path)
    return journal


def create_recovery_journal(path: Path, prepared: JsonObject) -> JsonObject:
    """Publish the prepared authority once before any physical side effect."""
    write_no_replace(path, _raw(prepared), mode=MODE_PRIVATE)
    regular_identity(path, mode=MODE_PRIVATE)
    return prepared


def replace_recovery_journal(path: Path, journal: JsonObject) -> None:
    """Durably replace one validated journal transition."""
    write_atomic_replace(path, _raw(journal))
    regular_identity(path, mode=MODE_PRIVATE)


def validate_recovery_replay(
    journal: JsonObject,
    prepared: JsonObject,
) -> None:
    """Require every immutable prepared field and action plan unchanged."""
    if set(journal) != set(prepared) or journal.get("schema_version") != 1:
        _fail("stale recovery journal has an open or unknown root")
    for key, value in prepared.items():
        if key not in MUTABLE_KEYS and journal.get(key) != value:
            _fail(f"stale recovery immutable field drifted: {key}")
    actions = journal.get("resource_actions")
    completed = journal.get("completed_action_ids")
    if not isinstance(actions, list) or not all(
        isinstance(item, dict) for item in actions
    ):
        _fail("stale recovery actions are not a closed object array")
    if not isinstance(completed, list) or not all(
        isinstance(item, str) for item in completed
    ):
        _fail("stale completed actions are not a string array")
    action_objects = cast("list[JsonObject]", actions)
    completed_ids = cast("list[str]", completed)
    prefix = [item.get("action_id") for item in action_objects[: len(completed_ids)]]
    if completed_ids != prefix:
        _fail("stale completed actions are not an ordered prefix")
    validate_runner_recovery_prefixes(journal)


def _raw(journal: JsonObject) -> bytes:
    return canonical_bytes(journal)


def _fail(message: str) -> Never:
    raise IsolationError(message)
