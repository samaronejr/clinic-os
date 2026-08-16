from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing import cgroup_capability_probe as capability
from ops.testing.approved_plan import freeze_plan
from ops.testing.cgroup_capability_probe import ProbeRequest
from ops.testing.cgroup_probe_records import _initial_journal
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    JsonObject,
    canonical_bytes,
    fsync_directory,
    raw_sha256,
    stable_lock,
    stat_identity,
    write_no_replace,
)
from ops.testing.isolation_snapshot import SnapshotRequest, snapshot_ledger

from isolation_probe_fixtures import complete_probe

if TYPE_CHECKING:
    import pytest

FOUNDATION_SHA = "a" * 40
ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROBE_PID = 2_000_000_001


def _barrier_probe_request(
    tmp_path: Path,
) -> tuple[ProbeRequest, Path, JsonObject]:
    attempt_root = tmp_path / ATTEMPT_ID
    attempt_root.mkdir()
    parent = tmp_path / "delegated-parent"
    parent.mkdir()
    parent_value = parent.stat()
    identity: JsonObject = {
        "device": parent_value.st_dev,
        "gid": os.getegid(),
        "inode": parent_value.st_ino,
        "uid": os.geteuid(),
    }
    proof: JsonObject = {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "cgroup_parent_identity": identity,
        "cgroup_parent_path": str(parent),
        "cgroup_relative_path": "/delegated-parent",
    }
    proof_raw = canonical_bytes(proof)
    proof_path = tmp_path / "proof.json"
    write_no_replace(proof_path, proof_raw, mode=MODE_IMMUTABLE)
    return (
        ProbeRequest(
            ATTEMPT_ID,
            attempt_root,
            proof_path,
            raw_sha256(proof_raw),
            "todo1-kickoff",
        ),
        parent,
        identity,
    )


def test_child_barrier_does_not_fsync_the_cgroup_parent_after_rmdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a deterministic child barrier whose cgroup parent rejects fsync.
    request, parent, identity = _barrier_probe_request(tmp_path)
    child = parent / f"clinic-os-phase1a-probe-{ATTEMPT_ID}-todo1-kickoff"
    child.mkdir()
    journal = _initial_journal(request, parent, identity, child)
    journal_path = request.attempt_root / "execution-host-probes/todo1-kickoff.json"
    journal_path.parent.mkdir()
    write_no_replace(journal_path, canonical_bytes(journal), mode=MODE_PRIVATE)
    expected_relative = f"/delegated-parent/{child.name}"
    transitions: list[str] = []

    def transition(_path: Path, record: JsonObject, state: str) -> None:
        record["state"] = state
        transitions.append(state)

    def unsupported_parent_fsync(path: Path) -> None:
        if path == parent:
            raise OSError(errno.EINVAL, "simulated cgroup2 directory fsync")
        fsync_directory(path)

    monkeypatch.setattr(os, "fork", lambda: PROBE_PID)
    monkeypatch.setattr(
        capability,
        "_read_child_identity",
        lambda _descriptor: {
            "expected_parent_pid": os.getpid(),
            "probe_pgid": PROBE_PID,
            "probe_pid": PROBE_PID,
            "probe_start_ticks": 9_999,
        },
    )
    monkeypatch.setattr(capability, "_validate_child_identity", lambda *_args: None)
    monkeypatch.setattr(capability, "_write_control", lambda *_args: None)
    monkeypatch.setattr(capability, "_process_cgroup", lambda _pid: expected_relative)
    monkeypatch.setattr(capability, "_write_all", lambda *_args: None)
    monkeypatch.setattr(capability, "_read_exact_byte", lambda _descriptor: b"1")
    monkeypatch.setattr(capability, "_cgroup_members", lambda _child: [PROBE_PID])
    monkeypatch.setattr(capability, "_wait_child", lambda _pid: None)
    monkeypatch.setattr(capability, "_wait_unpopulated", lambda _child: None)
    monkeypatch.setattr(capability, "_transition", transition)
    monkeypatch.setattr(capability, "fsync_directory", unsupported_parent_fsync)

    # When: the public probe engine completes the normal child barrier.
    capability._run_child_barrier(
        journal_path,
        journal,
        child,
        "/delegated-parent",
    )

    # Then: journal durability reaches removed without fsyncing the cgroup directory.
    assert transitions == [
        "probe-identity",
        "probe-migrated",
        "probe-running",
        "kill-complete",
        "remove-intent",
        "removed",
    ]
    assert not child.exists()
    assert stat.S_IMODE(journal_path.stat().st_mode) == MODE_IMMUTABLE


def test_snapshot_adopts_exact_interrupted_lock_and_plan_prefix(
    tmp_path: Path,
) -> None:
    # Given: a crash left the exact stable lock and plan copies but no ledger.
    authority_workspace = tmp_path / "authority"
    authority_root = authority_workspace / ".omo"
    worktree = tmp_path / "feature"
    authority_root.mkdir(parents=True)
    worktree.mkdir()
    source = tmp_path / "approved.md"
    source.write_bytes(b"# approved\n")
    tracked = worktree / "docs/plans/clinic-os-phase1a-approved.md"
    freeze_plan(source, authority_root, tracked, tracked.with_suffix(".sha256"))
    lock_path = authority_root / "evidence/isolation-ledger-phase1a.lock"
    with stable_lock(lock_path, create=True):
        pass
    lock_identity = stat_identity(lock_path)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    proof_path = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
    proof: JsonObject = {
        "boot_id": boot_id,
        "cgroup_parent_identity": {
            "device": 1,
            "gid": os.getegid(),
            "inode": 2,
            "link_count": 2,
            "mode": 0o755,
            "uid": os.geteuid(),
        },
        "cgroup_parent_path": "/sys/fs/cgroup/test",
        "foundation_sha": FOUNDATION_SHA,
        "workspace_realpath": str(authority_workspace),
    }
    write_no_replace(proof_path, canonical_bytes(proof), mode=MODE_IMMUTABLE)

    # When: the identical first snapshot is retried.
    ledger_path = snapshot_ledger(
        SnapshotRequest(
            approved_plan=source,
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
        ),
        probe_runner=complete_probe,
    )

    # Then: one ledger binds the unchanged lock and existing immutable plan bytes.
    assert ledger_path.exists()
    assert stat_identity(lock_path) == lock_identity
    assert tracked.read_bytes() == source.read_bytes()
