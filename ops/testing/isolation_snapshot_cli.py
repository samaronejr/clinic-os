"""Parse and dispatch both exact isolation snapshot command forms."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.cgroup_capability_probe import ProbeRequest, run_capability_probe
from ops.testing.isolation_after_archive import snapshot_after_archive
from ops.testing.isolation_after_archive_records import AfterArchiveSnapshotRequest
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_snapshot import SnapshotRequest, snapshot_ledger
from ops.testing.isolation_tracked_snapshot import (
    TrackedSnapshotRequest,
    snapshot_tracked_ledger,
)

if TYPE_CHECKING:
    from collections.abc import Callable

FIRST_ARGUMENT_COUNT: Final = 13
AFTER_ARCHIVE_ARGUMENT_COUNT: Final = 15
TRACKED_ARGUMENT_COUNT: Final = 9
CONTROL_CHARACTER_LIMIT: Final = 32


def dispatch_snapshot_command(
    arguments: tuple[str, ...],
    inventory_reader: Callable[[], JsonObject] | None = None,
    probe_runner: Callable[[ProbeRequest], Path] | None = None,
) -> bool:
    """Dispatch one exact initial or post-archive snapshot grammar."""
    if not arguments or arguments[0] != "snapshot":
        return False
    if arguments[:2] == ("snapshot", "--after-archive"):
        closed, plan, workspace, root, foundation, worktree, proof = (
            _parse_after_archive(arguments)
        )
        snapshot_after_archive(
            AfterArchiveSnapshotRequest(
                closed_attempt=closed,
                approved_plan=plan,
                authority_workspace=workspace,
                authority_root=root,
                foundation_sha=foundation,
                worktree=worktree,
                execution_host_preflight=proof,
                inventory_fixture=_inventory_fixture(inventory_reader),
            ),
            probe_runner=(
                run_capability_probe if probe_runner is None else probe_runner
            ),
        )
        return True
    if "--tracked-ci-sidecar" in arguments:
        plan, sidecar, foundation, worktree = _parse_tracked(arguments)
        evidence_root = _tracked_evidence_root()
        snapshot_tracked_ledger(
            TrackedSnapshotRequest(
                approved_plan=plan,
                tracked_ci_sidecar=sidecar,
                foundation_sha=foundation,
                worktree=worktree,
                evidence_root=evidence_root,
                inventory_fixture=_inventory_fixture(inventory_reader),
            )
        )
        return True
    plan, workspace, root, foundation, worktree, proof = _parse_first(arguments)
    snapshot_ledger(
        SnapshotRequest(
            approved_plan=plan,
            authority_workspace=workspace,
            authority_root=root,
            foundation_sha=foundation,
            worktree=worktree,
            execution_host_preflight=proof,
            inventory_fixture=_inventory_fixture(inventory_reader),
        ),
        probe_runner=run_capability_probe if probe_runner is None else probe_runner,
    )
    return True


def _inventory_fixture(
    inventory_reader: Callable[[], JsonObject] | None,
) -> JsonObject | None:
    return None if inventory_reader is None else inventory_reader()


def _parse_first(
    arguments: tuple[str, ...],
) -> tuple[Path, Path, Path, str, Path, Path]:
    expected = (
        "snapshot",
        "--approved-plan",
        "--authority-workspace",
        "--authority-root",
        "--foundation-sha",
        "--worktree",
        "--execution-host-preflight",
    )
    received = tuple(arguments[index] for index in (0, 1, 3, 5, 7, 9, 11))
    if len(arguments) != FIRST_ARGUMENT_COUNT or received != expected:
        _fail("invalid isolation-ledger command grammar")
    return (
        Path(arguments[2]),
        Path(arguments[4]),
        Path(arguments[6]),
        arguments[8],
        Path(arguments[10]),
        Path(arguments[12]),
    )


def _parse_tracked(
    arguments: tuple[str, ...],
) -> tuple[Path, Path, str, Path]:
    expected = (
        "snapshot",
        "--approved-plan",
        "--tracked-ci-sidecar",
        "--foundation-sha",
        "--worktree",
    )
    received = tuple(arguments[index] for index in (0, 1, 3, 5, 7))
    if len(arguments) != TRACKED_ARGUMENT_COUNT or received != expected:
        _fail("invalid isolation-ledger command grammar")
    return (
        Path(arguments[2]),
        Path(arguments[4]),
        arguments[6],
        Path(arguments[8]),
    )


def _tracked_evidence_root() -> Path:
    value = os.environ.get("CLINIC_EVIDENCE_ROOT")
    if (
        value is None
        or not value
        or any(ord(character) < CONTROL_CHARACTER_LIMIT for character in value)
    ):
        _fail("CLINIC_EVIDENCE_ROOT is required for a tracked-CI snapshot")
    return Path(value)


def _parse_after_archive(
    arguments: tuple[str, ...],
) -> tuple[str, Path, Path, Path, str, Path, Path]:
    expected = (
        "snapshot",
        "--after-archive",
        "--approved-plan",
        "--authority-workspace",
        "--authority-root",
        "--foundation-sha",
        "--worktree",
        "--execution-host-preflight",
    )
    received = tuple(arguments[index] for index in (0, 1, 3, 5, 7, 9, 11, 13))
    if len(arguments) != AFTER_ARCHIVE_ARGUMENT_COUNT or received != expected:
        _fail("invalid isolation-ledger command grammar")
    return (
        arguments[2],
        Path(arguments[4]),
        Path(arguments[6]),
        Path(arguments[8]),
        arguments[10],
        Path(arguments[12]),
        Path(arguments[14]),
    )


def _fail(message: str) -> Never:
    raise IsolationError(message)
