"""Authenticate and resume an interrupted post-ledger initial snapshot."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, cast

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    directory_identity,
    load_json,
    regular_identity,
)
from ops.testing.isolation_snapshot_records import (
    SnapshotRootInputs,
    baseline_record,
    root_record,
)
from ops.testing.shared_evidence_baseline import capture_manifest

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_namespace import NamespaceBinding


TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
PREFIX_ENTRIES: Final = {
    "execution-host-probes",
    "final-failure-receipts",
    "receipt-lineage-seed.json",
    "shared-evidence-baseline.json",
    "todo-evidence",
}


@dataclass(frozen=True, slots=True)
class RecoveredSnapshot:
    """Authenticated paths and bytes needed to finish initial publication."""

    attempt_id: str
    attempt_root: Path
    shared_path: Path
    shared_raw: bytes


@dataclass(frozen=True, slots=True)
class SnapshotRecoveryInputs:
    """Current immutable inputs used to authenticate a published ledger."""

    ledger_path: Path
    lock_path: Path
    binding: NamespaceBinding
    worktree: Path
    foundation_sha: str
    boot_id: str
    proof: JsonObject
    inventory: JsonObject
    approved_plan: JsonObject
    lock_identity: JsonObject


def authenticate_interrupted_snapshot(
    inputs: SnapshotRecoveryInputs,
) -> RecoveredSnapshot:
    """Validate one recoverable canonical ledger against current inputs."""
    regular_identity(inputs.ledger_path, mode=MODE_PRIVATE)
    ledger, _raw = load_json(inputs.ledger_path)
    attempt_id = _attempt_id(ledger.get("attempt_id"))
    evidence_root = inputs.ledger_path.parent
    attempt_root = evidence_root / "clinic-os-phase1a-runtime" / attempt_id
    shared_path = attempt_root / "shared-evidence-baseline.json"
    _validate_task_paths(evidence_root, attempt_id)
    _validate_attempt_prefix(attempt_root)
    manifest = capture_manifest(evidence_root, attempt_id=attempt_id)
    baseline, shared_raw = baseline_record(
        inputs.binding,
        inputs.inventory,
        manifest,
        shared_path,
    )
    recorded_baseline = _object(ledger.get("baseline"))
    for key in ("evidence_lstat", "omo_lstat"):
        recorded_identity = _object(recorded_baseline.get(key))
        created = recorded_identity.get("created_by_attempt")
        if not isinstance(created, bool):
            _fail("interrupted ledger has an invalid namespace creation flag")
        _object(baseline[key])["created_by_attempt"] = created
    created_at = ledger.get("created_at_utc")
    if not isinstance(created_at, str) or TIMESTAMP.fullmatch(created_at) is None:
        _fail("interrupted ledger has an invalid creation timestamp")
    expected = root_record(
        SnapshotRootInputs(
            attempt_id=attempt_id,
            foundation_sha=inputs.foundation_sha,
            binding=inputs.binding,
            worktree=inputs.worktree,
            boot_id=inputs.boot_id,
            created_at=created_at,
            lock_path=inputs.lock_path,
            lock_identity=inputs.lock_identity,
            attempt_root=attempt_root,
            approved_plan=inputs.approved_plan,
            proof=inputs.proof,
            baseline=baseline,
            inventory=inputs.inventory,
        )
    )
    if ledger != expected:
        _fail("interrupted canonical ledger differs from authenticated inputs")
    return RecoveredSnapshot(attempt_id, attempt_root, shared_path, shared_raw)


def _validate_task_paths(evidence_root: Path, attempt_id: str) -> None:
    forbidden = (
        evidence_root / "clinic-os-phase1a-final",
        evidence_root / "clinic-os-phase1a-rejected" / attempt_id,
        evidence_root / "isolation-archive-rollover-phase1a.json",
        evidence_root / "isolation-archive-rollover-phase1a.sentinel",
        evidence_root / "isolation-ledger-final-phase1a.json",
    )
    if any(path.exists() or path.is_symlink() for path in forbidden):
        _fail("interrupted canonical ledger has task-owned terminal state")


def _validate_attempt_prefix(attempt_root: Path) -> None:
    if not attempt_root.exists() and not attempt_root.is_symlink():
        return
    if attempt_root.is_symlink() or attempt_root.resolve(strict=True) != attempt_root:
        _fail("interrupted attempt root is noncanonical")
    _private_directory(attempt_root)
    entries = {entry.name for entry in attempt_root.iterdir()}
    if not entries.issubset(PREFIX_ENTRIES):
        _fail("interrupted attempt root contains an unknown entry")
    _validate_empty_evidence_directories(attempt_root)
    _validate_probe_prefix(attempt_root / "execution-host-probes")


def _validate_empty_evidence_directories(attempt_root: Path) -> None:
    for name in ("final-failure-receipts", "todo-evidence"):
        path = attempt_root / name
        if path.exists() or path.is_symlink():
            _private_directory(path)
            if any(path.iterdir()):
                _fail("interrupted attempt root contains task evidence")


def _validate_probe_prefix(probe_root: Path) -> None:
    if probe_root.exists() or probe_root.is_symlink():
        _private_directory(probe_root)
        probe_entries = {entry.name for entry in probe_root.iterdir()}
        if not probe_entries.issubset({"todo1-kickoff.json"}):
            _fail("interrupted kickoff probe contains an unknown entry")
        journal_path = probe_root / "todo1-kickoff.json"
        if journal_path.exists() or journal_path.is_symlink():
            journal, _raw = load_json(journal_path)
            if journal.get("state") == "removed":
                regular_identity(journal_path, mode=MODE_IMMUTABLE)
                _fail("canonical isolation ledger already exists")


def _private_directory(path: Path) -> None:
    if directory_identity(path)["mode"] != MODE_DIRECTORY:
        _fail(f"interrupted directory mode drifted: {path.name}")


def _attempt_id(value: object) -> str:
    if not isinstance(value, str):
        _fail("interrupted ledger attempt ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        message = "interrupted ledger attempt ID is invalid"
        raise IsolationError(message) from error
    if str(parsed) != value:
        _fail("interrupted ledger attempt ID is invalid")
    return value


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail("interrupted ledger object is invalid")
    return cast("JsonObject", value)


def _fail(message: str) -> Never:
    raise IsolationError(message)
