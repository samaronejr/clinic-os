"""Build filesystem records unique to authority-free tracked-CI snapshots."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    raw_sha256,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class TrackedBaselineInputs:
    """Filesystem and inventory values for an authority-free hosted baseline."""

    evidence_root: Path
    evidence_created: bool
    inventory: JsonObject
    shared_manifest: JsonObject
    shared_path: Path


def tracked_baseline_record(
    inputs: TrackedBaselineInputs,
) -> tuple[JsonObject, bytes]:
    """Bind one job-scoped evidence directory without inventing a .omo link."""
    shared_raw = canonical_bytes(inputs.shared_manifest)
    value = inputs.evidence_root.stat(follow_symlinks=False)
    baseline: JsonObject = {
        "containers": inputs.inventory["containers"],
        "evidence_lstat": {
            "created_by_attempt": inputs.evidence_created,
            "device": value.st_dev,
            "gid": value.st_gid,
            "inode": value.st_ino,
            "mode": stat.S_IMODE(value.st_mode),
            "path": str(inputs.evidence_root),
            "realpath": str(inputs.evidence_root),
            "type": "directory",
            "uid": value.st_uid,
        },
        "listeners": inputs.inventory["listeners"],
        "networks": inputs.inventory["networks"],
        "omo_lstat": None,
        "shared_evidence_manifest": {
            "entry_count": _integer(inputs.shared_manifest["entry_count"]),
            "path": str(inputs.shared_path),
            "sha256": raw_sha256(shared_raw),
        },
        "volumes": inputs.inventory["volumes"],
    }
    return baseline, shared_raw


def _integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        message = "expected an integer"
        raise IsolationError(message)
    return value
