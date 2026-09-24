from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from ops.testing.isolation_archive import archive_rollover
from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    write_no_replace,
)

from isolation.isolation_archive_fixtures import closed_archive_fixture
from isolation.isolation_rejection_fixtures import empty_inventory
from isolation_claim_fixtures import snapshot

CRASH_STAGES = (
    "prepared",
    "sentinel-created",
    "control-moved",
    "attempt-moved",
    "ledger-moved",
    "tombstone-published",
    "relocating",
    "journal-relocated",
    "complete",
)


def test_snapshot_creates_the_first_immutable_receipt_lineage_seed(
    tmp_path: Path,
) -> None:
    # Given / When: the first attempt snapshot creates its authenticated root.
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    seed_path = Path(str(ledger["attempt_root"])) / "receipt-lineage-seed.json"

    # Then: the no-predecessor lineage seed is exact and immutable.
    seed, _ = load_json(seed_path)
    assert seed_path.stat().st_mode & 0o777 == 0o400
    assert seed == {
        "current_attempt_id": ledger["attempt_id"],
        "fix_sources": [],
        "next_fix_sequence": 1,
        "previous_attempt_id": None,
        "previous_bundle_path": None,
        "previous_lineage_sha256": None,
        "previous_tombstone_sha256": None,
        "primary_sources": [],
        "schema_version": 1,
    }


def test_archive_rollover_publishes_exact_complete_bundle_and_replays(
    tmp_path: Path,
) -> None:
    # Given: one closed rejected attempt with complete receipt lineage.
    fixture = closed_archive_fixture(tmp_path)
    lock_path = fixture.ledger_path.with_name("isolation-ledger-phase1a.lock")
    lock_inode = lock_path.stat().st_ino

    # When: archive rollover completes and is replayed after hypothetical stdout loss.
    bundle = archive_rollover(
        fixture.ledger_path,
        fixture.request,
        inventory_reader=empty_inventory,
    )
    replayed = archive_rollover(
        fixture.ledger_path,
        fixture.request,
        inventory_reader=empty_inventory,
    )

    # Then: sources are absent and the immutable bundle binds every required hash.
    assert replayed == bundle
    assert not fixture.ledger_path.exists()
    assert not fixture.control_root.exists()
    assert not fixture.attempt_root.exists()
    assert lock_path.stat().st_ino == lock_inode
    assert {entry.name for entry in bundle.iterdir()} == {
        "archive-state.json",
        "attempt",
        "control",
        "ledger.closed.json",
        "rejection.json",
    }
    journal, _ = load_json(bundle / "archive-state.json")
    tombstone, _ = load_json(bundle / "rejection.json")
    assert journal["state"] == "complete"
    assert tombstone["control_tree_sha256"] == fixture.control_tree_sha256
    assert tombstone["attempt_tree_sha256"] == fixture.attempt_tree_sha256
    assert tombstone["ledger_sha256"] == fixture.ledger_sha256
    assert tombstone["receipt_lineage_seed_sha256"] == fixture.seed_sha256
    assert tombstone["lineage_validation_sha256"] == fixture.validation_sha256
    assert tombstone["receipt_lineage_sha256"] == fixture.lineage_sha256
    assert tombstone["rejections"] == [{"failure_class": "finding", "lane": "F1"}]
    assert tombstone["rejecting_lanes"] == ["F1"]
    assert tombstone["retry_kind"] == "source-fix"
    assert not fixture.ledger_path.with_name(
        "isolation-archive-rollover-phase1a.json"
    ).exists()
    assert not fixture.ledger_path.with_name(
        "isolation-archive-rollover-phase1a.sentinel"
    ).exists()


@pytest.mark.parametrize("crash_stage", CRASH_STAGES)
def test_archive_rollover_replays_every_durable_crash_prefix(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    # Given: a closed attempt and one selected durable archive boundary.
    fixture = closed_archive_fixture(tmp_path)

    def crash(stage: str, _journal: JsonObject) -> None:
        if stage == crash_stage:
            message = f"simulated archive crash at {stage}"
            raise RuntimeError(message)

    # When: rollover crashes once and the exact request is replayed.
    with pytest.raises(RuntimeError, match="simulated archive crash"):
        archive_rollover(
            fixture.ledger_path,
            fixture.request,
            inventory_reader=empty_inventory,
            checkpoint=crash,
        )
    bundle = archive_rollover(
        fixture.ledger_path,
        fixture.request,
        inventory_reader=empty_inventory,
    )

    # Then: replay converges to one complete journal with no stable sentinel.
    journal, _ = load_json(bundle / "archive-state.json")
    assert journal["state"] == "complete"
    assert not fixture.ledger_path.with_name(
        "isolation-archive-rollover-phase1a.sentinel"
    ).exists()


def test_archive_refuses_missing_lineage_before_preparing_markers(
    tmp_path: Path,
) -> None:
    # Given: a closed rejected attempt whose final receipt lineage was removed.
    fixture = closed_archive_fixture(tmp_path)
    (fixture.attempt_root / "receipt-lineage.json").unlink()
    journal_path = fixture.ledger_path.with_name(
        "isolation-archive-rollover-phase1a.json"
    )
    sentinel_path = fixture.ledger_path.with_name(
        "isolation-archive-rollover-phase1a.sentinel"
    )

    # When: archive validates the complete lineage before preparing state.
    with pytest.raises(IsolationError, match="lineage"):
        archive_rollover(
            fixture.ledger_path,
            fixture.request,
            inventory_reader=empty_inventory,
        )

    # Then: no marker or source relocation occurred.
    assert not journal_path.exists()
    assert not sentinel_path.exists()
    assert fixture.ledger_path.exists()
    assert fixture.attempt_root.exists()


def test_archive_refuses_an_orphan_sentinel_without_mutating_sources(
    tmp_path: Path,
) -> None:
    # Given: a closed attempt and an unauthenticated stable sentinel name.
    fixture = closed_archive_fixture(tmp_path)
    sentinel_path = fixture.ledger_path.with_name(
        "isolation-archive-rollover-phase1a.sentinel"
    )
    write_no_replace(
        sentinel_path,
        canonical_bytes({"schema_version": 1}),
        mode=MODE_PRIVATE,
    )

    # When: archive classifies the impossible sentinel-only prefix.
    with pytest.raises(IsolationError, match="sentinel"):
        archive_rollover(
            fixture.ledger_path,
            fixture.request,
            inventory_reader=empty_inventory,
        )

    # Then: the ledger, attempt, and control sources remain in place.
    assert fixture.ledger_path.exists()
    assert fixture.attempt_root.exists()
    assert fixture.control_root.exists()


def test_archive_cli_replays_only_the_exact_closed_grammar(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    # Given: a rejected attempt bound to its feature-worktree authority link.
    fixture = closed_archive_fixture(tmp_path)
    ledger, _ = load_json(fixture.ledger_path)
    module = importlib.import_module("ops.testing.isolation_ledger")
    monkeypatch.chdir(Path(str(ledger["worktree_realpath"])))  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "capture_host_inventory",
        empty_inventory,
    )
    arguments = [
        "archive-rollover",
        "--closed-attempt",
        fixture.request.closed_attempt,
        "--rejected-sha",
        fixture.request.rejected_sha,
        "--control-root",
        ".omo/evidence/clinic-os-phase1a-final",
        "--rejection-spec-sha256",
        fixture.request.rejection_spec_sha256,
    ]

    # When: the exact form completes, replays without a canonical ledger, and one
    # reordered spelling is attempted.
    assert module.run_cli(arguments) == 0
    assert module.run_cli(arguments) == 0
    reordered = list(arguments)
    reordered[1], reordered[3] = reordered[3], reordered[1]
    assert module.run_cli(reordered) == 2

    # Then: the completed bundle remains the sole archived result.
    bundle = (
        fixture.ledger_path.parent
        / "clinic-os-phase1a-rejected"
        / fixture.request.closed_attempt
        / fixture.request.rejected_sha
    )
    journal, _ = load_json(bundle / "archive-state.json")
    assert journal["state"] == "complete"
