"""Build and publish the authenticated successor-ledger records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.approved_plan import ApprovedPlan, verify_plan
from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    regular_identity,
    utc_now,
    write_no_replace,
)
from ops.testing.isolation_snapshot_records import (
    ROOT_KEYS,
    SnapshotRootInputs,
    root_record,
)

if TYPE_CHECKING:
    from ops.testing.isolation_after_archive_validation import ValidatedRetryArchive
    from ops.testing.isolation_namespace import NamespaceBinding


@dataclass(frozen=True, slots=True)
class AfterArchiveSnapshotRequest:
    """Authenticated selectors for one archived-attempt successor snapshot."""

    closed_attempt: str
    approved_plan: Path
    authority_workspace: Path
    authority_root: Path
    foundation_sha: str
    worktree: Path
    execution_host_preflight: Path
    inventory_fixture: object | None = None


@dataclass(frozen=True, slots=True)
class SuccessorLedgerInputs:
    """Closed values required to construct one successor ledger root."""

    request: AfterArchiveSnapshotRequest
    binding: NamespaceBinding
    lock_path: Path
    lock_identity: JsonObject
    attempt_id: str
    attempt_root: Path
    approved_plan: JsonObject
    proof: JsonObject
    baseline: JsonObject
    inventory: JsonObject


def validate_successor_request(
    request: AfterArchiveSnapshotRequest,
    binding: NamespaceBinding,
    archived: ValidatedRetryArchive,
) -> JsonObject:
    """Bind every caller selector to the authenticated archived ledger."""
    ledger = archived.ledger
    authority = ledger.get("authority_binding")
    plan = ledger.get("approved_plan")
    if not isinstance(authority, dict) or not isinstance(plan, dict):
        _fail("archived ledger lacks closed authority records")
    expected = (
        ledger.get("attempt_id"),
        ledger.get("foundation_sha"),
        ledger.get("worktree_realpath"),
        authority.get("authority_workspace_realpath"),
        authority.get("authority_root_realpath"),
    )
    received = (
        request.closed_attempt,
        request.foundation_sha,
        str(request.worktree),
        str(binding.authority_workspace),
        str(binding.authority_root),
    )
    if expected != received:
        _fail("after-archive selectors differ from the archived ledger")
    approved = _approved_plan(plan)
    if request.approved_plan != approved.path:
        _fail("after-archive approved plan differs from archived authority")
    tracked = request.worktree / "docs" / "plans" / "clinic-os-phase1a-approved.md"
    verify_plan(approved, tracked, tracked.with_suffix(".sha256"))
    return plan


def successor_ledger_record(
    ledger_path: Path,
    inputs: SuccessorLedgerInputs,
) -> JsonObject:
    """Build the exact first-state root for a successor attempt or replay."""
    created_at = utc_now()
    if ledger_path.exists():
        existing, _raw = load_json(ledger_path)
        value = existing.get("created_at_utc")
        if not isinstance(value, str):
            _fail("successor ledger creation timestamp is invalid")
        created_at = value
    return root_record(
        SnapshotRootInputs(
            attempt_id=inputs.attempt_id,
            foundation_sha=inputs.request.foundation_sha,
            binding=inputs.binding,
            worktree=inputs.request.worktree,
            boot_id=_current_boot_id(),
            created_at=created_at,
            lock_path=inputs.lock_path,
            lock_identity=inputs.lock_identity,
            attempt_root=inputs.attempt_root,
            approved_plan=inputs.approved_plan,
            proof=inputs.proof,
            baseline=inputs.baseline,
            inventory=inputs.inventory,
        )
    )


def publish_or_replay_successor_ledger(
    ledger_path: Path,
    expected: JsonObject,
) -> None:
    """No-replace publish or byte-validate one crash-replayed successor root."""
    if set(expected) != ROOT_KEYS:
        _fail("internal successor ledger root keys drifted")
    raw = canonical_bytes(expected)
    try:
        write_no_replace(ledger_path, raw, mode=MODE_PRIVATE)
    except FileExistsError:
        regular_identity(ledger_path, mode=MODE_PRIVATE)
        _existing, observed = load_json(ledger_path)
        if observed != raw:
            _fail("existing successor ledger differs from retry snapshot")


def _approved_plan(value: JsonObject) -> ApprovedPlan:
    expected = {"path", "sha256", "sidecar_path", "source_kind"}
    if set(value) != expected or not all(
        isinstance(value.get(key), str) for key in expected
    ):
        _fail("archived approved-plan record is invalid")
    return ApprovedPlan(
        source_kind=str(value["source_kind"]),
        path=Path(str(value["path"])),
        sha256=str(value["sha256"]),
        sidecar_path=Path(str(value["sidecar_path"])),
    )


def _current_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _fail(message: str) -> Never:
    raise IsolationError(message)
