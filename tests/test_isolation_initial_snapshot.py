from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_inventory import normalize_inventory

from isolation_probe_fixtures import complete_probe


def test_initial_snapshot_publishes_the_exact_open_root_once(tmp_path: Path) -> None:
    # Given: immutable plan/proof inputs and a closed fixture.
    authority_workspace = tmp_path / "authority"
    authority_root = authority_workspace / ".omo"
    worktree = tmp_path / "feature"
    authority_root.mkdir(parents=True)
    worktree.mkdir()
    source_plan = tmp_path / "approved.md"
    source_plan.write_bytes(b"# approved\n")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    proof_path = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
    proof = {
        "boot_id": boot_id,
        "cgroup_parent_identity": {
            "device": 1,
            "gid": 1000,
            "inode": 2,
            "link_count": 2,
            "mode": 0o755,
            "uid": 1000,
        },
        "cgroup_parent_path": "/sys/fs/cgroup/test",
        "foundation_sha": "a" * 40,
        "workspace_realpath": str(authority_workspace),
    }
    proof_path.write_bytes(
        (json.dumps(proof, separators=(",", ":"), sort_keys=True) + "\n").encode()
    )
    proof_path.chmod(0o400)
    fixture: JsonObject = {
        "containers": [],
        "listeners": [],
        "networks": [],
        "volumes": [],
    }

    # When: the first snapshot implementation publishes beneath the sole authority root.
    try:
        module = importlib.import_module("ops.testing.isolation_ledger")
    except ModuleNotFoundError:
        pytest.fail("initial snapshot CLI is missing")
    request = module.SnapshotRequest(
        approved_plan=source_plan,
        authority_workspace=authority_workspace,
        authority_root=authority_root,
        foundation_sha="a" * 40,
        worktree=worktree,
        execution_host_preflight=proof_path,
        inventory_fixture=fixture,
    )
    ledger_path = module.snapshot_ledger(request, probe_runner=complete_probe)

    # Then: the canonical root is closed, open, immutable-input-bound, and no-replace.
    ledger = json.loads(ledger_path.read_text())
    assert set(ledger) == {
        "approved_plan",
        "attempt_id",
        "attempt_root",
        "authority_binding",
        "baseline",
        "boot_id",
        "boot_observation",
        "claims",
        "closed_at_utc",
        "created_at_utc",
        "execution_host_preflight",
        "foundation_sha",
        "last_verified_at_utc",
        "lock_identity",
        "lock_path",
        "reboot_stable_baseline_sha256",
        "rejection_close",
        "schema_version",
        "state",
        "worktree_realpath",
    }
    assert ledger["state"] == "open"
    assert ledger["claims"] == []
    assert ledger["closed_at_utc"] is None
    assert ledger_path.stat().st_mode & 0o777 == 0o600
    retry = module.SnapshotRequest(
        approved_plan=source_plan,
        authority_workspace=authority_workspace,
        authority_root=authority_root,
        foundation_sha="a" * 40,
        worktree=worktree,
        execution_host_preflight=proof_path,
        inventory_fixture=fixture,
    )
    with pytest.raises((FileExistsError, IsolationError)):
        module.snapshot_ledger(retry, probe_runner=complete_probe)


def test_initial_snapshot_cli_accepts_an_already_normalized_live_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the literal local grammar and the normalized result of a live reader.
    authority_workspace = tmp_path / "authority"
    authority_root = authority_workspace / ".omo"
    worktree = tmp_path / "feature"
    authority_root.mkdir(parents=True)
    worktree.mkdir()
    source_plan = tmp_path / "approved.md"
    source_plan.write_bytes(b"# approved\n")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    proof_path = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
    proof = {
        "boot_id": boot_id,
        "cgroup_parent_identity": {
            "device": 1,
            "gid": 1000,
            "inode": 2,
            "link_count": 2,
            "mode": 0o755,
            "uid": 1000,
        },
        "cgroup_parent_path": "/sys/fs/cgroup/test",
        "foundation_sha": "a" * 40,
        "workspace_realpath": str(authority_workspace),
    }
    proof_path.write_bytes(
        (json.dumps(proof, separators=(",", ":"), sort_keys=True) + "\n").encode()
    )
    proof_path.chmod(0o400)
    live_inventory = normalize_inventory(
        {
            "containers": [
                {
                    "config_user": "",
                    "health": None,
                    "id": "b" * 64,
                    "image_id": "sha256:" + "c" * 64,
                    "labels": [],
                    "mount_targets": [],
                    "network_mode": "none",
                    "published_ports": [],
                    "restart_count": 0,
                    "state": "exited",
                }
            ],
            "listeners": [],
            "networks": [],
            "volumes": [],
        }
    )
    ledger = importlib.import_module("ops.testing.isolation_ledger")
    environment = importlib.import_module("ops.testing.isolation_snapshot_environment")
    host_inventory = importlib.import_module("ops.testing.isolation_host_inventory")
    snapshot_cli = importlib.import_module("ops.testing.isolation_snapshot_cli")

    def live_reader() -> JsonObject:
        return live_inventory

    monkeypatch.setattr(ledger, "capture_host_inventory", live_reader)
    monkeypatch.setattr(environment, "capture_host_inventory", live_reader)
    monkeypatch.setattr(
        host_inventory,
        "capture_docker_metadata",
        lambda: pytest.fail("initial snapshot CLI reached ambient Docker inventory"),
    )
    monkeypatch.setattr(snapshot_cli, "run_capability_probe", complete_probe)

    # When: the public CLI dispatches the exact thirteen-argument snapshot form.
    exit_code = ledger.run_cli(
        [
            "snapshot",
            "--approved-plan",
            str(source_plan),
            "--authority-workspace",
            str(authority_workspace),
            "--authority-root",
            str(authority_root),
            "--foundation-sha",
            "a" * 40,
            "--worktree",
            str(worktree),
            "--execution-host-preflight",
            str(proof_path),
        ]
    )

    # Then: the normalized live result is consumed directly, not as a raw fixture.
    assert exit_code == 0
    published = json.loads(
        (authority_root / "evidence/isolation-ledger-phase1a.json").read_text()
    )
    assert published["baseline"]["containers"] == live_inventory["containers"]
