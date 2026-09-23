from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from ops.testing.isolation_after_archive import (
    AfterArchiveSnapshotRequest,
    snapshot_after_archive,
)
from ops.testing.isolation_archive import archive_rollover
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
)
from ops.testing.isolation_inventory import normalize_inventory

from isolation.isolation_archive_fixtures import (
    ClosedArchiveFixture,
    closed_archive_fixture,
)
from isolation.isolation_rejection_fixtures import empty_inventory
from isolation_claim_fixtures import FOUNDATION_SHA
from isolation_probe_fixtures import complete_probe


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


def test_after_archive_live_inventory_is_not_normalized_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the normalized result returned by the ambient host reader.
    module = importlib.import_module("ops.testing.isolation_after_archive")
    inventory = _normalized_live_inventory()
    monkeypatch.setattr(module, "capture_host_inventory", lambda: inventory)

    # When / Then: the live branch consumes that closed projection directly.
    assert module._inventory(None) == inventory


def _request(
    fixture: ClosedArchiveFixture,
    bundle: Path,
) -> AfterArchiveSnapshotRequest:
    archived, _ = load_json(bundle / "ledger.closed.json")
    binding = archived["authority_binding"]
    plan = archived["approved_plan"]
    proof = archived["execution_host_preflight"]
    assert isinstance(binding, dict)
    assert isinstance(plan, dict)
    assert isinstance(proof, dict)
    return AfterArchiveSnapshotRequest(
        closed_attempt=fixture.request.closed_attempt,
        approved_plan=Path(str(plan["path"])),
        authority_workspace=Path(str(binding["authority_workspace_realpath"])),
        authority_root=Path(str(binding["authority_root_realpath"])),
        foundation_sha=FOUNDATION_SHA,
        worktree=Path(str(archived["worktree_realpath"])),
        execution_host_preflight=Path(str(proof["path"])),
        inventory_fixture=empty_inventory(),
    )


def test_snapshot_after_archive_links_history_and_replays_without_duplication(
    tmp_path: Path,
) -> None:
    # Given: one fully validated rejection bundle and its retained stable lock.
    fixture = closed_archive_fixture(tmp_path)
    bundle = archive_rollover(
        fixture.ledger_path,
        fixture.request,
        inventory_reader=empty_inventory,
    )
    request = _request(fixture, bundle)
    archived, _ = load_json(bundle / "ledger.closed.json")
    tombstone, tombstone_raw = load_json(bundle / "rejection.json")

    # When: the successor snapshot completes and is replayed after stdout loss.
    ledger_path = snapshot_after_archive(request, probe_runner=complete_probe)
    first_raw = ledger_path.read_bytes()
    replayed = snapshot_after_archive(request, probe_runner=complete_probe)

    # Then: one open attempt retains the lock and links immutable prior history.
    assert replayed == ledger_path
    assert replayed.read_bytes() == first_raw
    ledger, _ = load_json(ledger_path)
    assert ledger["state"] == "open"
    assert ledger["attempt_id"] != archived["attempt_id"]
    assert ledger["lock_identity"] == archived["lock_identity"]
    assert ledger["approved_plan"] == archived["approved_plan"]
    assert ledger["execution_host_preflight"] == archived["execution_host_preflight"]
    probe_path = (
        Path(str(ledger["attempt_root"]))
        / "execution-host-probes"
        / "todo1-kickoff.json"
    )
    probe, _ = load_json(probe_path)
    assert probe["attempt_id"] == ledger["attempt_id"]
    assert probe["purpose"] == "todo1-kickoff"
    assert probe["state"] == "removed"
    seed_path = Path(str(ledger["attempt_root"])) / "receipt-lineage-seed.json"
    seed, _ = load_json(seed_path)
    assert seed_path.stat().st_mode & 0o777 == 0o400
    assert seed == {
        "current_attempt_id": ledger["attempt_id"],
        "fix_sources": [],
        "next_fix_sequence": 1,
        "previous_attempt_id": archived["attempt_id"],
        "previous_bundle_path": (
            ".omo/evidence/clinic-os-phase1a-rejected/"
            f"{archived['attempt_id']}/{tombstone['sha']}"
        ),
        "previous_lineage_sha256": tombstone["receipt_lineage_sha256"],
        "previous_tombstone_sha256": raw_sha256(tombstone_raw),
        "primary_sources": [
            {
                "attempt_id": archived["attempt_id"],
                "bundle_path": (
                    ".omo/evidence/clinic-os-phase1a-rejected/"
                    f"{archived['attempt_id']}/{tombstone['sha']}"
                ),
                "primary_commit_sha": f"{todo:040x}",
                "relative_path": (
                    f"todo-evidence/task-{todo}"
                    "-clinic-os-phase-1a-staff-scheduling.json"
                ),
                "sha256": f"{todo:064x}",
                "todo": todo,
            }
            for todo in range(1, 21)
        ],
        "schema_version": 1,
    }
    runtime_root = ledger_path.parent / "clinic-os-phase1a-runtime"
    assert sorted(path.name for path in runtime_root.iterdir()) == [
        str(ledger["attempt_id"])
    ]


def test_snapshot_after_archive_refuses_incomplete_bundle_before_new_ledger(
    tmp_path: Path,
) -> None:
    # Given: a completed rollover whose immutable archive journal is missing.
    fixture = closed_archive_fixture(tmp_path)
    bundle = archive_rollover(
        fixture.ledger_path,
        fixture.request,
        inventory_reader=empty_inventory,
    )
    request = _request(fixture, bundle)
    (bundle / "archive-state.json").unlink()

    # When / Then: history validation fails before any canonical ledger appears.
    with pytest.raises(IsolationError, match="archive"):
        snapshot_after_archive(request)
    assert not fixture.ledger_path.exists()


def test_snapshot_after_archive_cli_accepts_only_the_exact_grammar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one complete bundle and its exact successor snapshot arguments.
    fixture = closed_archive_fixture(tmp_path)
    bundle = archive_rollover(
        fixture.ledger_path,
        fixture.request,
        inventory_reader=empty_inventory,
    )
    request = _request(fixture, bundle)
    module = importlib.import_module("ops.testing.isolation_ledger")
    after_archive = importlib.import_module("ops.testing.isolation_after_archive")
    host_inventory = importlib.import_module("ops.testing.isolation_host_inventory")
    snapshot_cli = importlib.import_module("ops.testing.isolation_snapshot_cli")
    monkeypatch.chdir(request.worktree)
    monkeypatch.setattr(after_archive, "capture_host_inventory", empty_inventory)
    monkeypatch.setattr(
        host_inventory,
        "capture_docker_metadata",
        lambda: pytest.fail("after-archive CLI reached ambient Docker inventory"),
    )
    monkeypatch.setattr(snapshot_cli, "run_capability_probe", complete_probe)
    arguments = [
        "snapshot",
        "--after-archive",
        request.closed_attempt,
        "--approved-plan",
        str(request.approved_plan),
        "--authority-workspace",
        str(request.authority_workspace),
        "--authority-root",
        str(request.authority_root),
        "--foundation-sha",
        request.foundation_sha,
        "--worktree",
        str(request.worktree),
        "--execution-host-preflight",
        str(request.execution_host_preflight),
    ]

    # When: the exact form runs and an option-reordered spelling is attempted.
    assert module.run_cli(arguments) == 0
    reordered = list(arguments)
    reordered[3], reordered[5] = reordered[5], reordered[3]
    assert module.run_cli(reordered) == 2

    # Then: the exact command alone published the successor ledger.
    ledger, _ = load_json(fixture.ledger_path)
    assert ledger["state"] == "open"
    assert ledger["attempt_id"] != request.closed_attempt
