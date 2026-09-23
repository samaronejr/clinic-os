from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_inventory import normalize_inventory

if TYPE_CHECKING:
    from collections.abc import Callable


class _TrackedRequest(Protocol):
    approved_plan: Path
    tracked_ci_sidecar: Path
    foundation_sha: str
    worktree: Path
    evidence_root: Path
    inventory_fixture: object | None


FOUNDATION_SHA: Final = "a" * 40
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


def _empty_inventory() -> JsonObject:
    return {"containers": [], "listeners": [], "networks": [], "volumes": []}


def _normalized_live_inventory() -> JsonObject:
    return normalize_inventory(
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


def test_tracked_snapshot_live_inventory_is_not_normalized_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the normalized result returned by the hosted ambient reader.
    module = importlib.import_module("ops.testing.isolation_tracked_snapshot")
    host_inventory = importlib.import_module("ops.testing.isolation_host_inventory")
    inventory = _normalized_live_inventory()
    monkeypatch.setattr(
        module,
        "capture_host_inventory",
        lambda: inventory,
    )
    monkeypatch.setattr(
        host_inventory,
        "capture_docker_metadata",
        lambda: pytest.fail("tracked snapshot reached ambient Docker inventory"),
    )

    # When / Then: the live branch consumes that closed projection directly.
    assert module._inventory(None) == inventory


def _git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run(  # noqa: S603 - fixed executable and test-owned repository.
        (executable, "-C", str(root), *arguments),
        check=True,
        capture_output=True,
    )


def _fresh_checkout(root: Path) -> tuple[Path, Path]:
    plan = root / "docs" / "plans" / "clinic-os-phase1a-approved.md"
    sidecar = plan.with_suffix(".sha256")
    plan.parent.mkdir(parents=True)
    raw = b"# approved tracked plan\n"
    digest = hashlib.sha256(raw).hexdigest()
    plan.write_bytes(raw)
    sidecar.write_text(f"{digest}\n", encoding="ascii")
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Clinic Test")
    _git(root, "config", "user.email", "clinic@example.invalid")
    _git(root, "add", "docs/plans")
    _git(root, "commit", "--quiet", "-m", "fixture")
    return plan, sidecar


def _schema_returncode(instance: JsonObject, path: Path) -> int:
    validator = shutil.which("jsonschema")
    assert validator is not None
    path.write_text(json.dumps(instance), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed validator and test-owned input.
        (
            validator,
            "-V",
            "Draft202012Validator",
            str(PROJECT_ROOT / "ops/testing/isolation-ledger.schema.json"),
            "-i",
            str(path),
        ),
        check=False,
        capture_output=True,
    )
    return result.returncode


def _tracked_arguments(plan: Path, sidecar: Path, worktree: Path) -> tuple[str, ...]:
    return (
        "snapshot",
        "--approved-plan",
        str(plan),
        "--tracked-ci-sidecar",
        str(sidecar),
        "--foundation-sha",
        FOUNDATION_SHA,
        "--worktree",
        str(worktree),
    )


def test_hosted_snapshot_dispatches_only_the_exact_tracked_ci_grammar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the exact hosted argv and its predeclared job-scoped evidence root.
    worktree = tmp_path / "checkout"
    plan = worktree / "docs/plans/clinic-os-phase1a-approved.md"
    sidecar = plan.with_suffix(".sha256")
    evidence_root = tmp_path / "job-evidence"
    calls: list[object] = []
    module = importlib.import_module("ops.testing.isolation_snapshot_cli")

    def snapshot(request: object) -> Path:
        calls.append(request)
        return evidence_root / "isolation-ledger-phase1a.json"

    monkeypatch.setenv("CLINIC_EVIDENCE_ROOT", str(evidence_root))
    monkeypatch.setattr(module, "snapshot_tracked_ledger", snapshot)

    # When: the hosted form is dispatched.
    dispatched = module.dispatch_snapshot_command(
        _tracked_arguments(plan, sidecar, worktree),
        _empty_inventory,
    )

    # Then: it reaches only the tracked snapshot adapter with the closed inputs.
    assert dispatched is True
    assert len(calls) == 1
    request = cast("_TrackedRequest", calls[0])
    assert request.approved_plan == plan
    assert request.tracked_ci_sidecar == sidecar
    assert request.foundation_sha == FOUNDATION_SHA
    assert request.worktree == worktree
    assert request.evidence_root == evidence_root
    assert request.inventory_fixture == _empty_inventory()


def test_tracked_ci_snapshot_uses_committed_inputs_without_local_authority(
    tmp_path: Path,
) -> None:
    # Given: a fresh checkout containing only the committed tracked plan pair.
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    plan, sidecar = _fresh_checkout(worktree)
    evidence_root = tmp_path / "job-evidence"
    module = importlib.import_module("ops.testing.isolation_tracked_snapshot")

    # When: the hosted snapshot publishes into its job-scoped evidence root.
    request = module.TrackedSnapshotRequest(
        approved_plan=plan,
        tracked_ci_sidecar=sidecar,
        foundation_sha=FOUNDATION_SHA,
        worktree=worktree,
        evidence_root=evidence_root,
        inventory_fixture=_empty_inventory(),
    )
    ledger_path = module.snapshot_tracked_ledger(request)

    # Then: the source is tracked-CI and no local authority or proof was invented.
    ledger = json.loads(ledger_path.read_text())
    assert ledger_path == evidence_root / "isolation-ledger-phase1a.json"
    assert ledger["approved_plan"] == {
        "path": str(plan),
        "sha256": sidecar.read_text().strip(),
        "sidecar_path": str(sidecar),
        "source_kind": "tracked-ci",
    }
    assert ledger["authority_binding"] is None
    assert ledger["execution_host_preflight"] is None
    assert ledger["baseline"]["omo_lstat"] is None
    assert not (worktree / ".omo").exists()
    assert _schema_returncode(ledger, tmp_path / "valid-ledger.json") == 0
    ledger["approved_plan"]["source_kind"] = "invoked"
    assert _schema_returncode(ledger, tmp_path / "invalid-ledger.json") != 0


def test_tracked_ci_snapshot_rejects_a_pair_changed_after_commit(
    tmp_path: Path,
) -> None:
    # Given: both working-tree files are changed to a self-consistent uncommitted pair.
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    plan, sidecar = _fresh_checkout(worktree)
    raw = b"# uncommitted replacement\n"
    digest = hashlib.sha256(raw).hexdigest()
    plan.write_bytes(raw)
    sidecar.write_text(f"{digest}\n", encoding="ascii")
    module = importlib.import_module("ops.testing.isolation_tracked_snapshot")
    request = module.TrackedSnapshotRequest(
        approved_plan=plan,
        tracked_ci_sidecar=sidecar,
        foundation_sha=FOUNDATION_SHA,
        worktree=worktree,
        evidence_root=tmp_path / "job-evidence",
        inventory_fixture=_empty_inventory(),
    )

    # When / Then: sidecar agreement cannot replace commit authentication.
    with pytest.raises(IsolationError, match="committed"):
        module.snapshot_tracked_ledger(request)


def test_hosted_snapshot_requires_a_canonical_evidence_root_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: hosted argv without its required job-scoped evidence-root declaration.
    module = importlib.import_module("ops.testing.isolation_snapshot_cli")
    monkeypatch.delenv("CLINIC_EVIDENCE_ROOT", raising=False)
    plan = tmp_path / "docs/plans/clinic-os-phase1a-approved.md"

    # When / Then: dispatch fails before reading live inventory or mutating paths.
    inventory_reads = 0

    def inventory() -> JsonObject:
        nonlocal inventory_reads
        inventory_reads += 1
        return _empty_inventory()

    with pytest.raises(IsolationError, match="CLINIC_EVIDENCE_ROOT"):
        module.dispatch_snapshot_command(
            _tracked_arguments(plan, plan.with_suffix(".sha256"), tmp_path),
            inventory,
        )
    assert inventory_reads == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda arguments: (*arguments, "--execution-host-preflight", "/proof.json"),
        lambda arguments: (
            arguments[0],
            arguments[1],
            arguments[2],
            "--authority-root",
            "/foreign-authority/.omo",
            *arguments[3:],
        ),
    ],
    ids=["local-proof", "authority-root"],
)
def test_hosted_snapshot_rejects_local_authority_aliases_before_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[tuple[str, ...]], tuple[str, ...]],
) -> None:
    # Given: a hosted command widened with one forbidden local-authority field.
    module = importlib.import_module("ops.testing.isolation_snapshot_cli")
    evidence_root = tmp_path / "job-evidence"
    monkeypatch.setenv("CLINIC_EVIDENCE_ROOT", str(evidence_root))
    plan = tmp_path / "docs/plans/clinic-os-phase1a-approved.md"
    inventory_reads = 0

    def inventory() -> JsonObject:
        nonlocal inventory_reads
        inventory_reads += 1
        return _empty_inventory()

    # When / Then: closed grammar rejects the alias before host inspection.
    with pytest.raises(IsolationError, match="grammar"):
        module.dispatch_snapshot_command(
            mutation(_tracked_arguments(plan, plan.with_suffix(".sha256"), tmp_path)),
            inventory,
        )
    assert inventory_reads == 0
