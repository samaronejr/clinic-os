from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject, stable_lock
from ops.testing.isolation_namespace import bind_namespace
from ops.testing.shared_evidence_baseline import capture_manifest

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


def test_isolation_ledger_contract_surface_exists() -> None:
    # Given: Todo 1 starts without an isolation-ledger implementation.
    expected_paths = (
        PROJECT_ROOT / "ops" / "testing" / "isolation_ledger.py",
        PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json",
        PROJECT_ROOT / "ops" / "testing" / "execution_host_preflight.py",
        PROJECT_ROOT / "ops" / "testing" / "shared_evidence_baseline.py",
        PROJECT_ROOT / "ops" / "testing" / "publish_todo_receipt.py",
        PROJECT_ROOT / "ops" / "testing" / "assert_foundation_history.py",
    )

    # When: the normative ledger surface is inspected before implementation.
    missing_paths = [
        path.relative_to(PROJECT_ROOT) for path in expected_paths if not path.is_file()
    ]

    # Then: the task cannot pass until every contract entrypoint exists.
    assert missing_paths == []


def test_namespace_binding_records_the_closed_authority_identity(
    tmp_path: Path,
) -> None:
    # Given: an executor-owned authority workspace and a feature worktree.
    authority_workspace = tmp_path / "authority"
    authority_root = authority_workspace / ".omo"
    worktree = tmp_path / "feature"
    authority_root.mkdir(parents=True)
    worktree.mkdir()

    # When: Todo 1 binds the feature worktree to the sole authority root.
    binding = bind_namespace(authority_workspace, authority_root, worktree)

    # Then: the persisted authority identity has the exact closed key set.
    assert set(binding.authority_root_identity) == {
        "device",
        "gid",
        "inode",
        "mode",
        "uid",
    }
    assert (worktree / ".omo").is_symlink()
    assert (worktree / ".omo").resolve(strict=True) == authority_root


def test_stable_lock_create_fails_closed_when_the_name_exists(tmp_path: Path) -> None:
    # Given: a pre-existing private regular file occupies the stable-lock name.
    lock_path = tmp_path / "isolation-ledger-phase1a.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)

    # When: first-ledger creation tries to claim that existing name.
    with (
        pytest.raises(IsolationError, match="stable lock already exists"),
        stable_lock(lock_path, create=True),
    ):
        pytest.fail("an existing stable-lock name was adopted as new")

    # Then: the pre-existing inode remains unchanged and private.
    assert lock_path.read_bytes() == b""
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_isolation_error_escapes_the_stable_lock_without_masking(
    tmp_path: Path,
) -> None:
    # Given: a newly authenticated stable-lock context.
    lock_path = tmp_path / "isolation-ledger-phase1a.lock"
    message = "intended boundary failure"

    # When: a typed boundary failure occurs while that lock is held.
    with (
        pytest.raises(IsolationError, match=message),
        stable_lock(lock_path, create=True),
    ):
        raise IsolationError(message)

    # Then: context-manager unwinding does not replace the typed error.


def test_shared_evidence_rejects_a_symlinked_evidence_root(tmp_path: Path) -> None:
    # Given: an attacker substitutes the evidence directory with a symlink.
    real_evidence = tmp_path / "real-evidence"
    real_evidence.mkdir()
    linked_evidence = tmp_path / "evidence"
    linked_evidence.symlink_to(real_evidence, target_is_directory=True)

    # When: the shared-evidence inventory opens the substituted root.
    with pytest.raises(IsolationError, match="evidence root"):
        capture_manifest(
            linked_evidence,
            attempt_id="12345678-1234-4123-8123-123456789abc",
        )

    # Then: no manifest or nested entry is created through the symlink.
    assert list(real_evidence.iterdir()) == []


def test_approved_plan_freeze_publishes_identical_immutable_copies(
    tmp_path: Path,
) -> None:
    # Given: one absolute regular invoked plan and empty local/tracked destinations.
    source = tmp_path / "approved.md"
    source.write_bytes(b"# approved\n")
    authority_root = tmp_path / ".omo"
    authority_root.mkdir()
    tracked_copy = tmp_path / "tracked" / "approved.md"
    tracked_sidecar = tracked_copy.with_suffix(".sha256")
    command = PROJECT_ROOT / "ops" / "testing" / "approved_plan.py"

    # When: the real freeze command publishes both authenticated copies.
    result = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned paths.
        (
            sys.executable,
            command,
            "freeze",
            "--source",
            source,
            "--authority-root",
            authority_root,
            "--tracked-copy",
            tracked_copy,
            "--tracked-sidecar",
            tracked_sidecar,
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: the command is quiet, succeeds, and leaves identical mode-0400 copies.
    assert result.returncode == 0, result.stderr
    frozen = authority_root / "evidence" / "review-inputs" / "approved-plan.md"
    assert frozen.read_bytes() == source.read_bytes() == tracked_copy.read_bytes()
    assert frozen.stat().st_mode & 0o777 == 0o400
    assert tracked_copy.stat().st_mode & 0o777 == 0o400
    assert tracked_sidecar.read_text().endswith("\n")


def test_inventory_normalizer_emits_only_the_closed_baseline_fields(
    tmp_path: Path,
) -> None:
    # Given: synthetic Docker metadata contains only plan-permitted inspect fields.
    fixture: JsonObject = {
        "containers": [
            {
                "config_user": "1000:1000",
                "health": None,
                "id": "a" * 64,
                "image_id": "sha256:" + ("b" * 64),
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
    fixture_path = tmp_path / "inventory.json"
    fixture_path.write_text(json.dumps(fixture))
    command = PROJECT_ROOT / "ops" / "testing" / "isolation_inventory.py"

    # When: the inventory normalizer crosses the fixture trust boundary.
    result = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned input.
        (sys.executable, command, "normalize-fixture", "--input", fixture_path),
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: only the ledger baseline fields survive and config is digest-bound.
    assert result.returncode == 0, result.stderr
    normalized = json.loads(result.stdout)
    assert set(normalized) == {"containers", "listeners", "networks", "volumes"}
    assert set(normalized["containers"][0]) == {
        "config_sha256",
        "health",
        "id",
        "labels",
        "published_ports",
        "restart_count",
        "state",
    }
