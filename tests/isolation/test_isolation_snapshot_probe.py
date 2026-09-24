from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ops.testing import cgroup_capability_probe as capability
from ops.testing import cgroup_probe_recovery as recovery
from ops.testing.cgroup_probe_records import _initial_journal
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    JsonObject,
    canonical_bytes,
    fsync_directory,
    load_json,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_snapshot import SnapshotRequest, snapshot_ledger

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.cgroup_capability_probe import ProbeRequest

ATTEMPT_ID_PROCESS = 2_000_000_001
ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FOUNDATION_SHA = "a" * 40


def _unsupported_parent_fsync(parent: Path) -> Callable[[Path], None]:
    def fsync(path: Path) -> None:
        if path == parent:
            raise OSError(errno.EINVAL, "simulated cgroup2 directory fsync")
        fsync_directory(path)

    return fsync


def _probe_request(
    tmp_path: Path,
) -> tuple[capability.ProbeRequest, Path, JsonObject]:
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
    request = capability.ProbeRequest(
        ATTEMPT_ID,
        attempt_root,
        proof_path,
        raw_sha256(proof_raw),
        "todo1-kickoff",
    )
    return request, parent, identity


def test_capability_creation_does_not_fsync_the_cgroup_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a fresh probe whose delegated parent rejects directory fsync.
    request, parent, _identity = _probe_request(tmp_path)
    monkeypatch.setattr(capability, "_run_child_barrier", lambda *_args: None)
    monkeypatch.setattr(
        capability,
        "fsync_directory",
        _unsupported_parent_fsync(parent),
    )

    # When: the probe durably records mkdir-intent and creates its child.
    journal_path = capability.run_capability_probe(request)

    # Then: journal state advances without treating cgroupfs as durable storage.
    assert load_json(journal_path)[0]["state"] == "child-ready"


def test_failed_probe_cleanup_does_not_fsync_the_cgroup_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an empty failed child and a parent that rejects directory fsync.
    request, parent, identity = _probe_request(tmp_path)
    child = parent / f"clinic-os-phase1a-probe-{ATTEMPT_ID}-todo1-kickoff"
    child.mkdir()
    child_value = child.stat()
    journal = _initial_journal(request, parent, identity, child)
    journal.update(
        {
            "child_device": child_value.st_dev,
            "child_inode": child_value.st_ino,
            "expected_parent_pid": os.getpid(),
            "probe_barrier_released": True,
            "probe_pgid": ATTEMPT_ID_PROCESS,
            "probe_pid": ATTEMPT_ID_PROCESS,
            "probe_start_ticks": 9_999,
            "state": "kill-complete",
        }
    )
    journal_path = request.attempt_root / "execution-host-probes/todo1-kickoff.json"
    journal_path.parent.mkdir()
    write_no_replace(journal_path, canonical_bytes(journal), mode=MODE_PRIVATE)
    monkeypatch.setattr(capability, "_cgroup_members", lambda _child: [])
    monkeypatch.setattr(
        capability,
        "fsync_directory",
        _unsupported_parent_fsync(parent),
    )

    # When: failed-probe cleanup durably records remove-intent and removes the child.
    capability._cleanup_failed_child(journal_path, journal, child)

    # Then: the journal retains the recoverable intent without a cgroupfs flush.
    assert load_json(journal_path)[0]["state"] == "remove-intent"
    assert not child.exists()


@pytest.mark.parametrize(
    ("state", "child_present", "expected_state"),
    [
        ("mkdir-intent", False, "child-ready"),
        ("remove-intent", True, "removed"),
    ],
)
def test_same_boot_recovery_does_not_fsync_the_cgroup_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    child_present: bool,
    expected_state: str,
) -> None:
    # Given: one recoverable cgroup prefix and an unsupported parent fsync.
    request, parent, identity = _probe_request(tmp_path)
    child = parent / f"clinic-os-phase1a-probe-{ATTEMPT_ID}-todo1-kickoff"
    journal = _initial_journal(request, parent, identity, child)
    if child_present:
        child.mkdir()
        child_value = child.stat()
        journal.update(
            {
                "child_device": child_value.st_dev,
                "child_inode": child_value.st_ino,
                "expected_parent_pid": os.getpid(),
                "probe_barrier_released": True,
                "probe_pgid": ATTEMPT_ID_PROCESS,
                "probe_pid": ATTEMPT_ID_PROCESS,
                "probe_start_ticks": 9_999,
            }
        )
    journal["state"] = state
    journal_path = request.attempt_root / "execution-host-probes/todo1-kickoff.json"
    journal_path.parent.mkdir()
    write_no_replace(journal_path, canonical_bytes(journal), mode=MODE_PRIVATE)
    monkeypatch.setattr(recovery, "_cgroup_members", lambda _child: [])
    monkeypatch.setattr(
        recovery,
        "fsync_directory",
        _unsupported_parent_fsync(parent),
    )

    # When: same-boot recovery advances the authenticated journal prefix.
    recovery.recover_same_boot_probe(journal_path, request, parent, identity)

    # Then: the durable state advances without requiring cgroupfs writeback.
    assert load_json(journal_path)[0]["state"] == expected_state


def test_snapshot_runs_kickoff_probe_after_ledger_publication(tmp_path: Path) -> None:
    # Given: one authenticated local proof and an inert host inventory.
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
    parent_identity: JsonObject = {
        "device": parent_value.st_dev,
        "gid": os.getegid(),
        "inode": parent_value.st_ino,
        "link_count": parent_value.st_nlink,
        "mode": parent_value.st_mode & 0o777,
        "uid": os.geteuid(),
    }
    proof: JsonObject = {
        "boot_id": boot_id,
        "cgroup_parent_identity": parent_identity,
        "cgroup_parent_path": str(delegated_parent),
        "foundation_sha": FOUNDATION_SHA,
        "workspace_realpath": str(authority_workspace),
    }
    proof_raw = canonical_bytes(proof)
    write_no_replace(proof_path, proof_raw, mode=MODE_IMMUTABLE)
    callback_observations: list[bool] = []

    def complete_probe(request: ProbeRequest) -> Path:
        ledger_path = authority_root / "evidence/isolation-ledger-phase1a.json"
        callback_observations.append(ledger_path.exists())
        assert request.purpose == "todo1-kickoff"
        assert request.proof_sha256 == raw_sha256(proof_raw)
        child = delegated_parent / (
            f"clinic-os-phase1a-probe-{request.attempt_id}-todo1-kickoff"
        )
        child.mkdir()
        child_value = child.stat()
        child.rmdir()
        journal = _initial_journal(request, delegated_parent, parent_identity, child)
        journal.update(
            {
                "child_device": child_value.st_dev,
                "child_inode": child_value.st_ino,
                "expected_parent_pid": os.getpid(),
                "probe_barrier_released": True,
                "probe_pgid": ATTEMPT_ID_PROCESS,
                "probe_pid": ATTEMPT_ID_PROCESS,
                "probe_start_ticks": 9_999,
                "removal_kind": "rmdir",
                "removed_at_utc": journal["updated_at_utc"],
                "state": "removed",
            }
        )
        path = request.attempt_root / "execution-host-probes/todo1-kickoff.json"
        path.parent.mkdir()
        write_no_replace(path, canonical_bytes(journal), mode=MODE_IMMUTABLE)
        return path

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

    # When: the canonical local snapshot is published.
    ledger_path = snapshot_ledger(request, probe_runner=complete_probe)

    # Then: the exact kickoff journal is immutable and only ran after the ledger.
    ledger, _ = load_json(ledger_path)
    probe_path = Path(str(ledger["attempt_root"])) / (
        "execution-host-probes/todo1-kickoff.json"
    )
    assert callback_observations == [True]
    assert probe_path.stat().st_mode & 0o777 == MODE_IMMUTABLE
    assert load_json(probe_path)[0]["state"] == "removed"
