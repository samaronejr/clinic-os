"""Read the ambient inputs used by initial isolation snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_host_inventory import capture_host_inventory
from ops.testing.isolation_inventory import normalize_inventory
from ops.testing.isolation_snapshot_records import execution_proof_record

if TYPE_CHECKING:
    from ops.testing.isolation_snapshot_types import _SnapshotState


def snapshot_inventory(fixture: object | None) -> JsonObject:
    """Normalize a test fixture or capture the current protected inventory."""
    if fixture is not None:
        return normalize_inventory(fixture)
    return capture_host_inventory()


def current_boot_id() -> str:
    """Read the current kernel boot identifier."""
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def entry_present(path: Path) -> bool:
    """Classify regular and dangling directory entries as present."""
    return path.exists() or path.is_symlink()


def validate_execution_proof(state: _SnapshotState) -> None:
    """Require the execution-host proof to remain byte-bound after probing."""
    request = state.request
    observed = execution_proof_record(
        request.execution_host_preflight,
        state.binding.authority_workspace,
        state.binding.authority_root,
        request.foundation_sha,
        state.boot_id,
    )
    if observed != state.proof:
        message = "execution-host proof changed during the kickoff probe"
        raise IsolationError(message)
