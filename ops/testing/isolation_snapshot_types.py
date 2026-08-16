"""Typed request and state records for initial isolation snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject
    from ops.testing.isolation_namespace import NamespaceBinding


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    """Authenticated inputs for first-ledger creation."""

    approved_plan: Path
    authority_workspace: Path
    authority_root: Path
    foundation_sha: str
    worktree: Path
    execution_host_preflight: Path
    inventory_fixture: object | None = None


@dataclass(frozen=True, slots=True)
class _SnapshotPaths:
    evidence_root: Path
    lock: Path
    ledger: Path
    tracked_plan: Path


@dataclass(frozen=True, slots=True)
class _SnapshotState:
    request: SnapshotRequest
    binding: NamespaceBinding
    proof: JsonObject
    boot_id: str
    inventory: JsonObject
    paths: _SnapshotPaths
    recovering: bool
    adopting: bool
    attempt_id: str


@dataclass(frozen=True, slots=True)
class _SnapshotMaterial:
    attempt_id: str
    attempt_root: Path
    shared_path: Path
    shared_raw: bytes
