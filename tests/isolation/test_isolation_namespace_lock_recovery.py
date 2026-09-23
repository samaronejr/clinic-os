from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ops.testing import isolation_snapshot
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    stable_lock,
    stat_identity,
    write_no_replace,
)
from ops.testing.isolation_snapshot import SnapshotRequest, snapshot_ledger

from isolation_probe_fixtures import complete_probe

if TYPE_CHECKING:
    from collections.abc import Iterator


FOUNDATION_SHA = "a" * 40
LOCK_NAME = "isolation-ledger-phase1a.lock"
LEDGER_NAME = "isolation-ledger-phase1a.json"
LOCK_BOUNDARIES = (
    "before-create",
    "after-create",
    "before-file-fsync",
    "after-file-fsync",
    "before-parent-fsync",
    "after-parent-fsync",
    "before-open",
    "after-open",
    "before-flock",
    "after-flock",
    "before-ledger",
    "after-ledger",
)
_REAL_OPEN = os.open
_REAL_CLOSE = os.close
_REAL_FSYNC = os.fsync
_REAL_FLOCK = fcntl.flock
_REAL_WRITE = write_no_replace
_REAL_STABLE_LOCK = stable_lock


class _SimulatedCrash(BaseException):
    pass


class _BoundaryCrashHarness:
    def __init__(
        self,
        boundary: str,
        lock_path: Path,
        evidence_root: Path,
        ledger_path: Path,
    ) -> None:
        self.boundary = boundary
        self.lock_path = lock_path
        self.evidence_root = evidence_root
        self.ledger_path = ledger_path
        self.fired = False
        self.descriptor_paths: dict[int, Path] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "open", self.open)
        monkeypatch.setattr(os, "close", self.close)
        monkeypatch.setattr(os, "fsync", self.fsync)
        monkeypatch.setattr(fcntl, "flock", self.flock)
        monkeypatch.setattr(isolation_snapshot, "write_no_replace", self.write)

    def crash(self, label: str) -> None:
        if not self.fired and self.boundary == label:
            self.fired = True
            raise _SimulatedCrash(label)

    def open(
        self,
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        normalized = Path(os.fsdecode(path))
        if normalized == self.lock_path:
            label = "before-create" if flags & os.O_CREAT else "before-open"
            self.crash(label)
        descriptor = _REAL_OPEN(path, flags, mode, dir_fd=dir_fd)
        self.descriptor_paths[descriptor] = normalized
        try:
            if normalized == self.lock_path:
                label = "after-create" if flags & os.O_CREAT else "after-open"
                self.crash(label)
        except _SimulatedCrash:
            _REAL_CLOSE(descriptor)
            self.descriptor_paths.pop(descriptor, None)
            raise
        return descriptor

    def close(self, descriptor: int) -> None:
        self.descriptor_paths.pop(descriptor, None)
        _REAL_CLOSE(descriptor)

    def fsync(self, descriptor: int) -> None:
        descriptor_path = self.descriptor_paths.get(descriptor)
        boundaries = {
            self.lock_path: ("before-file-fsync", "after-file-fsync"),
            self.evidence_root: ("before-parent-fsync", "after-parent-fsync"),
        }
        labels = (
            boundaries.get(descriptor_path) if descriptor_path is not None else None
        )
        if labels is None:
            _REAL_FSYNC(descriptor)
            return
        self.crash(labels[0])
        _REAL_FSYNC(descriptor)
        self.crash(labels[1])

    def flock(self, descriptor: int, operation: int) -> None:
        if self.descriptor_paths.get(descriptor) != self.lock_path:
            _REAL_FLOCK(descriptor, operation)
            return
        self.crash("before-flock")
        _REAL_FLOCK(descriptor, operation)
        self.crash("after-flock")

    def write(self, path: Path, raw: bytes, *, mode: int) -> None:
        if path != self.ledger_path:
            _REAL_WRITE(path, raw, mode=mode)
            return
        self.crash("before-ledger")
        _REAL_WRITE(path, raw, mode=mode)
        self.crash("after-ledger")


def _request(tmp_path: Path) -> tuple[SnapshotRequest, Path, Path]:
    authority_workspace = tmp_path / "authority"
    authority_root = authority_workspace / ".omo"
    worktree = tmp_path / "feature"
    delegated_parent = tmp_path / "delegated-parent"
    authority_root.mkdir(parents=True)
    worktree.mkdir()
    delegated_parent.mkdir()
    plan = tmp_path / "approved.md"
    plan.write_bytes(b"# approved\n")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    proof_path = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
    parent_value = delegated_parent.stat()
    proof: JsonObject = {
        "boot_id": boot_id,
        "cgroup_parent_identity": {
            "device": parent_value.st_dev,
            "gid": parent_value.st_gid,
            "inode": parent_value.st_ino,
            "link_count": parent_value.st_nlink,
            "mode": parent_value.st_mode & 0o777,
            "uid": parent_value.st_uid,
        },
        "cgroup_parent_path": str(delegated_parent),
        "foundation_sha": FOUNDATION_SHA,
        "workspace_realpath": str(authority_workspace),
    }
    write_no_replace(proof_path, canonical_bytes(proof), mode=MODE_IMMUTABLE)
    request = SnapshotRequest(
        approved_plan=plan,
        authority_workspace=authority_workspace,
        authority_root=authority_root,
        foundation_sha=FOUNDATION_SHA,
        worktree=worktree,
        execution_host_preflight=proof_path,
        inventory_fixture={
            "containers": [],
            "listeners": [],
            "networks": [],
            "volumes": [],
        },
    )
    return request, authority_root / "evidence", worktree


@pytest.mark.parametrize("boundary", LOCK_BOUNDARIES)
def test_snapshot_recovers_every_initial_lock_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    # Given: owner death is injected at one stable-lock or ledger boundary.
    request, evidence_root, _worktree = _request(tmp_path)
    lock_path = evidence_root / LOCK_NAME
    ledger_path = evidence_root / LEDGER_NAME
    harness = _BoundaryCrashHarness(boundary, lock_path, evidence_root, ledger_path)
    harness.install(monkeypatch)

    # When: the interrupted snapshot is retried with the identical inputs.
    with pytest.raises(_SimulatedCrash, match=boundary):
        snapshot_ledger(request, probe_runner=complete_probe)
    interrupted_lock = stat_identity(lock_path) if lock_path.exists() else None
    recovered = snapshot_ledger(request, probe_runner=complete_probe)

    # Then: one stable inode and one authenticated canonical ledger converge.
    assert harness.fired
    assert recovered == ledger_path
    assert load_json(recovered)[0]["state"] == "open"
    if interrupted_lock is not None:
        assert stat_identity(lock_path) == interrupted_lock


def test_snapshot_rejects_namespace_replacement_before_ledger_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the feature authority symlink is replaced after initial binding.
    request, evidence_root, worktree = _request(tmp_path)
    foreign_root = tmp_path / "foreign-root"
    foreign_root.mkdir()
    original_lock = _REAL_STABLE_LOCK

    @contextmanager
    def replaced_namespace_lock(
        path: Path,
        *,
        create: bool,
    ) -> Iterator[tuple[int, JsonObject]]:
        with original_lock(path, create=create) as held:
            link = worktree / ".omo"
            link.unlink()
            link.symlink_to(foreign_root, target_is_directory=True)
            yield held

    monkeypatch.setattr(isolation_snapshot, "stable_lock", replaced_namespace_lock)

    # When: snapshot publication reaches the already-held stable lock.
    with pytest.raises(IsolationError, match="authority namespace binding drifted"):
        snapshot_ledger(request, probe_runner=complete_probe)

    # Then: no canonical ledger is published through the stale binding.
    assert not (evidence_root / LEDGER_NAME).exists()


def test_snapshot_rejects_lock_replacement_before_ledger_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the stable-lock name is replaced after its original inode is flocked.
    request, evidence_root, _worktree = _request(tmp_path)
    original_lock = _REAL_STABLE_LOCK

    @contextmanager
    def replaced_stable_lock(
        path: Path,
        *,
        create: bool,
    ) -> Iterator[tuple[int, JsonObject]]:
        with original_lock(path, create=create) as held:
            path.unlink()
            path.write_bytes(b"")
            path.chmod(0o600)
            yield held

    monkeypatch.setattr(isolation_snapshot, "stable_lock", replaced_stable_lock)

    # When: snapshot publication rechecks the held lock before creating JSON.
    with pytest.raises(IsolationError, match="stable lock identity changed"):
        snapshot_ledger(request, probe_runner=complete_probe)

    # Then: replacement is detected before canonical-ledger publication.
    assert not (evidence_root / LEDGER_NAME).exists()
