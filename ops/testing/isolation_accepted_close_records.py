"""Build and validate accepted-close journal and receipt records."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
)

if TYPE_CHECKING:
    from ops.testing.isolation_terminal_records import ValidatedTerminalRevalidation

STATE_KEYS: Final = {
    "approvals_sha256",
    "attempt_id",
    "closed_at_utc",
    "closed_ledger_sha256",
    "final_sha256",
    "inputs_sha256",
    "phase",
    "pre_f4_sha256",
    "preclose_ledger_sha256",
    "receipt_sha256",
    "reboot_stable_baseline_sha256",
    "schema_version",
    "sha",
    "terminal_revalidation_relative_path",
    "terminal_revalidation_sha256",
    "tree_sha",
    "updated_at_utc",
}
RECEIPT_KEYS: Final = {
    "approvals_sha256",
    "attempt_id",
    "closed_at_utc",
    "closed_ledger_sha256",
    "final_sha256",
    "inputs_sha256",
    "pre_f4_sha256",
    "reboot_stable_baseline_sha256",
    "schema_version",
    "sha",
    "terminal_revalidation_sha256",
    "tree_sha",
}
PHASES: Final = (
    "prepared",
    "ledger-closed",
    "receipt-published",
    "complete",
)
SHA256_KEYS: Final = {
    "approvals_sha256",
    "closed_ledger_sha256",
    "final_sha256",
    "inputs_sha256",
    "pre_f4_sha256",
    "preclose_ledger_sha256",
    "receipt_sha256",
    "reboot_stable_baseline_sha256",
    "terminal_revalidation_sha256",
}
SHA256_LENGTH: Final = 64
SHA40_LENGTH: Final = 40
UUID_PATTERN: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
TIMESTAMP_PATTERN: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
TERMINAL_RELATIVE_PATTERN: Final = re.compile(
    r"^terminal-revalidation/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$"
)
PRESERVED_REBIND_KEYS: Final = {
    "approvals_sha256",
    "attempt_id",
    "final_sha256",
    "inputs_sha256",
    "pre_f4_sha256",
    "reboot_stable_baseline_sha256",
    "schema_version",
    "sha",
    "tree_sha",
}


@dataclass(frozen=True, slots=True)
class PreparedAcceptedClose:
    """Exact prepared journal and the immutable target bytes it binds."""

    state: JsonObject
    closed_ledger: JsonObject
    closed_raw: bytes
    receipt: JsonObject
    receipt_raw: bytes


def prepare_accepted_close(
    ledger: JsonObject,
    ledger_raw: bytes,
    terminal: ValidatedTerminalRevalidation,
    *,
    closed_at_utc: str,
    updated_at_utc: str,
) -> PreparedAcceptedClose:
    """Derive closed ledger, receipt, and prepared journal from terminal bytes."""
    artifact = terminal.artifact
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    publisher, _raw = load_json(attempt_root / "final-wave-state.json")
    closed = copy.deepcopy(ledger)
    closed["state"] = "closed"
    closed["closed_at_utc"] = closed_at_utc
    closed_raw = canonical_bytes(closed)
    receipt: JsonObject = {
        "approvals_sha256": artifact["approvals_sha256"],
        "attempt_id": ledger["attempt_id"],
        "closed_at_utc": closed_at_utc,
        "closed_ledger_sha256": raw_sha256(closed_raw),
        "final_sha256": artifact["final_sha256"],
        "inputs_sha256": publisher["inputs_sha256"],
        "pre_f4_sha256": publisher["pre_f4_sha256"],
        "reboot_stable_baseline_sha256": ledger["reboot_stable_baseline_sha256"],
        "schema_version": 1,
        "sha": artifact["sha"],
        "terminal_revalidation_sha256": raw_sha256(terminal.artifact_raw),
        "tree_sha": artifact["tree_sha"],
    }
    receipt_raw = canonical_bytes(receipt)
    state: JsonObject = {
        "approvals_sha256": artifact["approvals_sha256"],
        "attempt_id": ledger["attempt_id"],
        "closed_at_utc": closed_at_utc,
        "closed_ledger_sha256": raw_sha256(closed_raw),
        "final_sha256": artifact["final_sha256"],
        "inputs_sha256": publisher["inputs_sha256"],
        "phase": "prepared",
        "pre_f4_sha256": publisher["pre_f4_sha256"],
        "preclose_ledger_sha256": raw_sha256(ledger_raw),
        "receipt_sha256": raw_sha256(receipt_raw),
        "reboot_stable_baseline_sha256": ledger["reboot_stable_baseline_sha256"],
        "schema_version": 1,
        "sha": artifact["sha"],
        "terminal_revalidation_relative_path": str(
            terminal.artifact_path.relative_to(attempt_root)
        ),
        "terminal_revalidation_sha256": raw_sha256(terminal.artifact_raw),
        "tree_sha": artifact["tree_sha"],
        "updated_at_utc": updated_at_utc,
    }
    if set(state) != STATE_KEYS or set(receipt) != RECEIPT_KEYS:
        _fail("internal accepted-close record root drifted")
    return PreparedAcceptedClose(state, closed, closed_raw, receipt, receipt_raw)


def validate_accepted_close_state(state: JsonObject) -> None:
    """Require the exact state root and one legal durable phase."""
    if (
        set(state) != STATE_KEYS
        or state.get("schema_version") != 1
        or state.get("phase") not in PHASES
    ):
        _fail("accepted-close state has an open or unknown root")
    for key in SHA256_KEYS:
        if not _sha256(state.get(key)):
            _fail(f"accepted-close {key} is not a SHA-256")
    attempt_id = state.get("attempt_id")
    sha = state.get("sha")
    tree_sha = state.get("tree_sha")
    terminal_relative = state.get("terminal_revalidation_relative_path")
    closed_at = state.get("closed_at_utc")
    updated_at = state.get("updated_at_utc")
    if not isinstance(attempt_id, str) or UUID_PATTERN.fullmatch(attempt_id) is None:
        _fail("accepted-close attempt ID is invalid")
    if not _sha40(sha) or not _sha40(tree_sha):
        _fail("accepted-close revision identity is invalid")
    if (
        not isinstance(terminal_relative, str)
        or TERMINAL_RELATIVE_PATTERN.fullmatch(terminal_relative) is None
    ):
        _fail("accepted-close terminal relative path is invalid")
    if (
        not isinstance(closed_at, str)
        or not isinstance(updated_at, str)
        or TIMESTAMP_PATTERN.fullmatch(closed_at) is None
        or TIMESTAMP_PATTERN.fullmatch(updated_at) is None
        or updated_at < closed_at
    ):
        _fail("accepted-close timestamps are invalid")


def receipt_from_state(state: JsonObject) -> tuple[JsonObject, bytes]:
    """Reconstruct the sole final receipt authorized by a prepared state."""
    receipt = {key: state[key] for key in RECEIPT_KEYS}
    receipt["schema_version"] = 1
    return receipt, canonical_bytes(receipt)


def preclose_from_closed(ledger: JsonObject) -> tuple[JsonObject, bytes]:
    """Reconstruct the exact open prefix changed only by accepted close."""
    result = copy.deepcopy(ledger)
    result["state"] = "open"
    result["closed_at_utc"] = None
    return result, canonical_bytes(result)


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha40(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA40_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
