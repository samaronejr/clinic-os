"""Build the terminal artifact and its exact refreshed ledger target."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

import rfc8785

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    canonical_sha256,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_terminal_artifact import ARTIFACT_KEYS
from ops.testing.isolation_terminal_publisher_contract import (
    validate_final_wave_journal,
)

INVENTORY_KEYS: Final = {"containers", "listeners", "networks", "volumes"}


@dataclass(frozen=True, slots=True)
class PreparedTerminalUpdate:
    """Immutable terminal artifact and the ledger bytes it precedes."""

    artifact: JsonObject
    artifact_raw: bytes
    ledger: JsonObject
    ledger_raw: bytes


@dataclass(frozen=True, slots=True)
class TerminalUpdateInputs:
    """Closed selectors and observations for one terminal update projection."""

    ledger_path: Path
    ledger: JsonObject
    ledger_raw: bytes
    final_path: Path
    current_boot: str
    observation: JsonObject
    accepted_predecessor_sha256: str | None


def prepare_terminal_update(inputs: TerminalUpdateInputs) -> PreparedTerminalUpdate:
    """Derive one terminal update without mutating its artifact or ledger paths."""
    ledger_path = inputs.ledger_path
    ledger = inputs.ledger
    ledger_raw = inputs.ledger_raw
    final_path = inputs.final_path
    current_boot = inputs.current_boot
    observation = inputs.observation
    _validate_observation(observation, current_boot, ledger)
    expected_final = ledger_path.parent / "clinic-os-phase1a-final" / "final.json"
    if final_path != expected_final or not final_path.is_absolute():
        _fail("terminal FINAL path is not canonical")
    regular_identity(final_path, mode=MODE_IMMUTABLE)
    final, final_raw = load_json(final_path)
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    journal_path = attempt_root / "final-wave-state.json"
    regular_identity(journal_path, mode=MODE_PRIVATE)
    journal, journal_raw = load_json(journal_path)
    validate_final_wave_journal(journal)
    if (
        journal.get("phase") != "final-frozen"
        or journal.get("terminal_publisher_state") != "released"
        or journal.get("attempt_id") != ledger.get("attempt_id")
        or journal.get("sha") != final.get("sha")
        or journal.get("control_root") != str(final_path.parent)
        or journal.get("final_sha256") != raw_sha256(final_raw)
    ):
        _fail("released terminal publisher differs from FINAL authority")
    post = copy.deepcopy(ledger)
    previous_boot = _text(ledger.get("boot_id"), "previous boot ID")
    post["boot_observation"] = copy.deepcopy(observation)
    post["last_verified_at_utc"] = observation["observed_at_utc"]
    if previous_boot != current_boot:
        post["boot_id"] = current_boot
    post_raw = canonical_bytes(post)
    baseline = _object(ledger.get("baseline"), "ledger baseline")
    shared = _object(
        baseline.get("shared_evidence_manifest"),
        "shared evidence manifest",
    )
    artifact: JsonObject = {
        "accepted_close_prepared_sha256": inputs.accepted_predecessor_sha256,
        "approvals_sha256": final["approvals_sha256"],
        "attempt_id": ledger["attempt_id"],
        "boot_changed": previous_boot != current_boot,
        "boot_observation": copy.deepcopy(observation),
        "boot_observation_sha256": raw_sha256(rfc8785.dumps(observation)),
        "current_boot_id": current_boot,
        "final_relative_path": "clinic-os-phase1a-final/final.json",
        "final_sha256": raw_sha256(final_raw),
        "lock_identity_sha256": canonical_sha256(ledger["lock_identity"]),
        "post_update_ledger_sha256": raw_sha256(post_raw),
        "pre_update_ledger_sha256": raw_sha256(ledger_raw),
        "previous_boot_id": previous_boot,
        "publisher_final_journal_sha256": raw_sha256(journal_raw),
        "purpose": "terminal-final",
        "reboot_stable_baseline_sha256": ledger["reboot_stable_baseline_sha256"],
        "schema_version": 1,
        "sha": final["sha"],
        "shared_evidence_manifest_sha256": shared["sha256"],
        "tree_sha": final["tree_sha"],
        "verified_at_utc": observation["observed_at_utc"],
    }
    if set(artifact) != ARTIFACT_KEYS:
        _fail("internal terminal artifact root drifted")
    return PreparedTerminalUpdate(
        artifact,
        canonical_bytes(artifact),
        post,
        post_raw,
    )


def terminal_observation(
    current_boot: str,
    inventory: JsonObject,
    observed_at_utc: str,
) -> JsonObject:
    """Close one fresh host inventory into the terminal boot observation."""
    if set(inventory) != INVENTORY_KEYS:
        _fail("terminal inventory has an open or unknown root")
    return {
        "boot_id": current_boot,
        "containers": copy.deepcopy(inventory["containers"]),
        "listeners": copy.deepcopy(inventory["listeners"]),
        "networks": copy.deepcopy(inventory["networks"]),
        "observed_at_utc": observed_at_utc,
        "volumes": copy.deepcopy(inventory["volumes"]),
    }


def _validate_observation(
    observation: JsonObject,
    current_boot: str,
    ledger: JsonObject,
) -> None:
    if set(observation) != INVENTORY_KEYS | {"boot_id", "observed_at_utc"}:
        _fail("terminal boot observation has an open root")
    timestamp = observation.get("observed_at_utc")
    previous = ledger.get("last_verified_at_utc")
    if (
        observation.get("boot_id") != current_boot
        or not isinstance(timestamp, str)
        or not isinstance(previous, str)
        or timestamp <= previous
    ):
        _fail("terminal boot observation is not fresh for the current boot")


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
