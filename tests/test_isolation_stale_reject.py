from __future__ import annotations

import importlib
from copy import deepcopy
from pathlib import Path
from typing import Protocol, cast

import pytest
import rfc8785
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_ledger_store import BOOT_ID_PATH

from isolation_claim_fixtures import (
    CLAIM_ID,
    FOUNDATION_SHA,
    claim_transitions,
    filesystem_spec,
    snapshot,
    write_spec,
)

PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"


class RejectCoordinator(Protocol):
    def reconcile_stale_boot(
        self,
        ledger_path: Path,
        *,
        inventory_reader: object | None = None,
        checkpoint: object | None = None,
        proof_publisher: object | None = None,
    ) -> JsonObject: ...


def _coordinator() -> RejectCoordinator:
    return cast(
        "RejectCoordinator",
        importlib.import_module("ops.testing.isolation_stale_recovery"),
    )


def _empty_inventory() -> JsonObject:
    return {"containers": [], "listeners": [], "networks": [], "volumes": []}


def _stale_receipted_ledger(tmp_path: Path) -> tuple[Path, Path]:
    ledger_path = snapshot(tmp_path)
    claim_transitions().reserve_claim(
        ledger_path,
        write_spec(tmp_path, filesystem_spec(CLAIM_ID, [])),
    )
    ledger, _ = load_json(ledger_path)
    ledger["boot_id"] = PREVIOUS_BOOT
    observation = ledger["boot_observation"]
    assert isinstance(observation, dict)
    observation["boot_id"] = PREVIOUS_BOOT
    write_atomic_replace(ledger_path, canonical_bytes(ledger))
    receipt_root = Path(str(ledger["attempt_root"])) / "final-failure-receipts"
    receipt_root.mkdir(mode=0o700, exist_ok=True)
    receipt: JsonObject = {
        "attempt_id": ledger["attempt_id"],
        "cause_code": "boot-changed",
        "cleanup_verified": True,
        "control_tree_sha256": None,
        "ended_at_utc": "2026-07-16T22:00:01.000002Z",
        "exit_code": None,
        "failure_class": "infrastructure",
        "journal_sha256": "d" * 64,
        "lane": "F1",
        "lineage_validation_sha256": "e" * 64,
        "observed_causes": ["boot-changed"],
        "output_sha256": None,
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "signal": None,
        "stage": "F1-review",
        "started_at_utc": "2026-07-16T22:00:00.000001Z",
        "timed_out": False,
    }
    receipt_path = receipt_root / "F1.json"
    write_no_replace(receipt_path, canonical_bytes(receipt), mode=MODE_IMMUTABLE)
    return ledger_path, receipt_path


def _forbid_resume_proof(*_args: object) -> JsonObject:
    message = "resume proof publisher invoked on rejection"
    raise AssertionError(message)


def _expected_receipt_aggregate(receipt_path: Path) -> str:
    entry = {
        "lane": "F1",
        "relative_path": "final-failure-receipts/F1.json",
        "sha256": raw_sha256(receipt_path.read_bytes()),
    }
    return raw_sha256(rfc8785.dumps([entry]))


def test_reject_binds_receipts_and_updated_ledger_before_boot_rebind(
    tmp_path: Path,
) -> None:
    # Given: a stale attempt carrying one immutable authenticated lane receipt.
    ledger_path, receipt_path = _stale_receipted_ledger(tmp_path)
    bound: list[tuple[JsonObject, bytes]] = []

    def observe_binding(stage: str, journal: JsonObject) -> None:
        if stage == "receipts-finalized":
            bound.append((deepcopy(journal), ledger_path.read_bytes()))

    # When: changed-boot recovery selects and completes the rejection branch.
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        checkpoint=observe_binding,
        proof_publisher=_forbid_resume_proof,
    )

    # Then: immutable receipt authority precedes the sole current-boot update.
    ledger, ledger_raw = load_json(ledger_path)
    assert len(bound) == 1
    journal, pre_update_raw = bound[0]
    assert raw_sha256(pre_update_raw) == journal["post_cleanup_ledger_sha256"]
    assert journal["failure_receipts_sha256"] == _expected_receipt_aggregate(
        receipt_path
    )
    assert journal["post_update_ledger_sha256"] == raw_sha256(ledger_raw)
    assert journal["resume_execution_host_preflight_path"] is None
    assert journal["resume_execution_host_preflight_sha256"] is None
    assert completed["state"] == "complete"
    assert ledger["claims"] == []
    assert ledger["boot_id"] == BOOT_ID_PATH.read_text().strip()


@pytest.mark.parametrize(
    "crash_stage",
    [
        "receipts-finalizing",
        "receipts-finalized",
        "reject-ledger-updated",
        "reject-boot-updated",
    ],
)
def test_reject_replays_each_no_publisher_crash_prefix(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    # Given: one selected durable crash boundary in receipt-only rejection.
    ledger_path, receipt_path = _stale_receipted_ledger(tmp_path)

    def crash(stage: str, _journal: JsonObject) -> None:
        if stage == crash_stage:
            message = f"simulated crash at {stage}"
            raise RuntimeError(message)

    # When: the first invocation crashes and the exact command is replayed.
    with pytest.raises(RuntimeError, match="simulated crash"):
        _coordinator().reconcile_stale_boot(
            ledger_path,
            inventory_reader=_empty_inventory,
            checkpoint=crash,
            proof_publisher=_forbid_resume_proof,
        )
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
        proof_publisher=_forbid_resume_proof,
    )

    # Then: replay preserves the selected aggregate and converges exactly once.
    _ledger, ledger_raw = load_json(ledger_path)
    assert completed["state"] == "complete"
    assert completed["failure_receipts_sha256"] == _expected_receipt_aggregate(
        receipt_path
    )
    assert raw_sha256(ledger_raw) == completed["post_update_ledger_sha256"]


def test_snapshot_precreates_private_failure_receipt_root(tmp_path: Path) -> None:
    # Given / When: the attempt snapshot is published before final-wave work.
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    root = Path(str(ledger["attempt_root"])) / "final-failure-receipts"

    # Then: the fixed receipt namespace already exists as private directory.
    assert root.is_dir()
    assert root.stat().st_mode & 0o777 == 0o700
