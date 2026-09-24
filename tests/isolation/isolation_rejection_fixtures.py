from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    canonical_bytes,
    load_json,
    write_no_replace,
)


def empty_inventory() -> JsonObject:
    return {"containers": [], "listeners": [], "networks": [], "volumes": []}


@dataclass(frozen=True, slots=True)
class FailureReceiptSpec:
    lane: str
    cause_code: str
    failure_class: str
    stage: str
    control_tree_sha256: str | None = None
    lineage_validation_sha256: str = "e" * 64


def write_failure_receipt(
    ledger_path: Path,
    spec: FailureReceiptSpec,
) -> Path:
    ledger, _ = load_json(ledger_path)
    receipt: JsonObject = {
        "attempt_id": ledger["attempt_id"],
        "cause_code": spec.cause_code,
        "cleanup_verified": True,
        "control_tree_sha256": spec.control_tree_sha256,
        "ended_at_utc": "2026-07-16T22:00:01.000002Z",
        "exit_code": None,
        "failure_class": spec.failure_class,
        "journal_sha256": "d" * 64,
        "lane": spec.lane,
        "lineage_validation_sha256": spec.lineage_validation_sha256,
        "observed_causes": [spec.cause_code],
        "output_sha256": None,
        "schema_version": 1,
        "sha": ledger["foundation_sha"],
        "signal": None,
        "stage": spec.stage,
        "started_at_utc": "2026-07-16T22:00:00.000001Z",
        "timed_out": False,
    }
    root = Path(str(ledger["attempt_root"])) / "final-failure-receipts"
    path = root / f"{spec.lane}.json"
    write_no_replace(path, canonical_bytes(receipt), mode=MODE_IMMUTABLE)
    return path
