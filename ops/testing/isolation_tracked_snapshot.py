"""Publish the authority-free first isolation snapshot used by tracked CI."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    directory_identity,
    ensure_private_directory,
    stable_lock,
    utc_now,
    write_no_replace,
)
from ops.testing.isolation_host_inventory import capture_host_inventory
from ops.testing.isolation_inventory import normalize_inventory
from ops.testing.isolation_lineage import publish_first_lineage_seed
from ops.testing.isolation_snapshot import (
    FOUNDATION_PATTERN,
    LEDGER_NAME,
    LOCK_NAME,
)
from ops.testing.isolation_snapshot_records import (
    ROOT_KEYS,
    SnapshotRootInputs,
    root_record,
)
from ops.testing.isolation_tracked_snapshot_records import (
    TrackedBaselineInputs,
    tracked_baseline_record,
)
from ops.testing.shared_evidence_baseline import capture_manifest
from ops.testing.tracked_approved_plan import authenticate_tracked_plan


@dataclass(frozen=True, slots=True)
class TrackedSnapshotRequest:
    """Committed hosted inputs for an authority-free first snapshot."""

    approved_plan: Path
    tracked_ci_sidecar: Path
    foundation_sha: str
    worktree: Path
    evidence_root: Path
    inventory_fixture: object | None = None


def snapshot_tracked_ledger(request: TrackedSnapshotRequest) -> Path:
    """Create the hosted ledger without a local authority binding or probe."""
    if FOUNDATION_PATTERN.fullmatch(request.foundation_sha) is None:
        _fail("foundation SHA must be lowercase 40-hex")
    approved = authenticate_tracked_plan(
        request.approved_plan,
        request.tracked_ci_sidecar,
        request.worktree,
    )
    evidence_created = _prepare_evidence_root(request.evidence_root)
    evidence_root = request.evidence_root
    ledger_path = evidence_root / LEDGER_NAME
    lock_path = evidence_root / LOCK_NAME
    adopting = lock_path.exists() or lock_path.is_symlink()
    if ledger_path.exists() or ledger_path.is_symlink():
        _fail("canonical isolation ledger already exists")
    if adopting:
        _validate_interrupted_prefix(evidence_root)
    attempt_id = str(uuid.uuid4())
    attempt_root = evidence_root / "clinic-os-phase1a-runtime" / attempt_id
    inventory = _inventory(request.inventory_fixture)
    boot_id = _current_boot_id()
    with stable_lock(lock_path, create=not adopting) as (_, lock_identity):
        if ledger_path.exists() or ledger_path.is_symlink():
            _fail("canonical isolation ledger appeared during bootstrap")
        if (
            authenticate_tracked_plan(
                request.approved_plan,
                request.tracked_ci_sidecar,
                request.worktree,
            )
            != approved
        ):
            _fail("tracked approved-plan binding changed during bootstrap")
        shared_manifest = capture_manifest(evidence_root, attempt_id=attempt_id)
        shared_path = attempt_root / "shared-evidence-baseline.json"
        baseline, shared_raw = tracked_baseline_record(
            TrackedBaselineInputs(
                evidence_root=evidence_root,
                evidence_created=evidence_created,
                inventory=inventory,
                shared_manifest=shared_manifest,
                shared_path=shared_path,
            )
        )
        created_at = utc_now()
        ledger = root_record(
            SnapshotRootInputs(
                attempt_id=attempt_id,
                foundation_sha=request.foundation_sha,
                binding=None,
                worktree=request.worktree,
                boot_id=boot_id,
                created_at=created_at,
                lock_path=lock_path,
                lock_identity=lock_identity,
                attempt_root=attempt_root,
                approved_plan=approved.as_json(),
                proof=None,
                baseline=baseline,
                inventory=inventory,
            )
        )
        if ledger.keys() != ROOT_KEYS:
            _fail("internal ledger root keys drifted")
        write_no_replace(ledger_path, canonical_bytes(ledger), mode=MODE_PRIVATE)
        _publish_attempt_inputs(attempt_root, shared_path, shared_raw, attempt_id)
        if (
            authenticate_tracked_plan(
                request.approved_plan,
                request.tracked_ci_sidecar,
                request.worktree,
            )
            != approved
        ):
            _fail("tracked approved-plan binding changed during publication")
    return ledger_path


def _prepare_evidence_root(path: Path) -> bool:
    if not path.is_absolute() or path.is_symlink():
        _fail("CLINIC_EVIDENCE_ROOT must be an absolute non-symlink directory")
    if not path.exists():
        parent = path.parent.resolve(strict=True)
        if parent != path.parent:
            _fail("CLINIC_EVIDENCE_ROOT parent is noncanonical")
        created = ensure_private_directory(path)
    else:
        if path.resolve(strict=True) != path:
            _fail("CLINIC_EVIDENCE_ROOT is noncanonical")
        created = False
    identity = directory_identity(path)
    if identity["mode"] != MODE_DIRECTORY:
        _fail("CLINIC_EVIDENCE_ROOT must have mode 0700")
    return created


def _validate_interrupted_prefix(evidence_root: Path) -> None:
    forbidden = (
        "clinic-os-phase1a-final",
        "clinic-os-phase1a-rejected",
        "clinic-os-phase1a-runtime",
        "isolation-archive-rollover-phase1a.json",
        "isolation-archive-rollover-phase1a.sentinel",
        "isolation-ledger-final-phase1a.json",
    )
    if any(
        (evidence_root / name).exists() or (evidence_root / name).is_symlink()
        for name in forbidden
    ):
        _fail("interrupted tracked-CI bootstrap has task-owned state")


def _publish_attempt_inputs(
    attempt_root: Path,
    shared_path: Path,
    raw: bytes,
    attempt_id: str,
) -> None:
    ensure_private_directory(attempt_root.parent)
    ensure_private_directory(attempt_root)
    ensure_private_directory(attempt_root / "final-failure-receipts")
    ensure_private_directory(attempt_root / "todo-evidence")
    write_no_replace(shared_path, raw, mode=MODE_IMMUTABLE)
    publish_first_lineage_seed(attempt_root, attempt_id)


def _inventory(fixture: object | None) -> JsonObject:
    if fixture is None:
        return capture_host_inventory()
    return normalize_inventory(fixture)


def _current_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _fail(message: str) -> Never:
    raise IsolationError(message)
