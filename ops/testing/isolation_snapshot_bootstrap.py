"""Guard the namespace, lock, and destinations of initial snapshot bootstrap."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    revalidate_held_lock,
)
from ops.testing.isolation_namespace_validation import validate_namespace_binding

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_namespace import NamespaceBinding


def revalidate_bootstrap(
    binding: NamespaceBinding,
    worktree: Path,
    lock_path: Path,
    lock_descriptor: int,
    lock_identity: JsonObject,
) -> None:
    """Revalidate the captured namespace and held stable-lock inode."""
    validate_namespace_binding(binding, worktree)
    revalidate_held_lock(lock_path, lock_descriptor, lock_identity)


def validate_interrupted_prefix(evidence_root: Path) -> None:
    """Accept only a task-state-free pre-ledger adoption prefix."""
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
        _fail("interrupted bootstrap has task-owned state")


def validate_snapshot_destinations(evidence_root: Path, attempt_id: str) -> None:
    """Require every destination for a new attempt to be absent."""
    destinations = (
        evidence_root / "clinic-os-phase1a-final",
        evidence_root / "clinic-os-phase1a-rejected" / attempt_id,
        evidence_root / "clinic-os-phase1a-runtime" / attempt_id,
        evidence_root / "isolation-archive-rollover-phase1a.json",
        evidence_root / "isolation-archive-rollover-phase1a.sentinel",
        evidence_root / "isolation-ledger-final-phase1a.json",
    )
    if any(path.exists() or path.is_symlink() for path in destinations):
        _fail("current snapshot destination already exists")


def require_recovery_plan_prefix(authority_root: Path, tracked_copy: Path) -> None:
    """Require every immutable plan artifact once a ledger is published."""
    frozen = authority_root / "evidence" / "review-inputs" / "approved-plan.md"
    paths = (
        frozen,
        frozen.with_suffix(".sha256"),
        tracked_copy,
        tracked_copy.with_suffix(".sha256"),
    )
    if any(not path.exists() or path.is_symlink() for path in paths):
        _fail("interrupted canonical ledger lacks its approved-plan prefix")


def _fail(message: str) -> Never:
    raise IsolationError(message)
