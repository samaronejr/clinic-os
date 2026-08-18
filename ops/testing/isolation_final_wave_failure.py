"""Publish journal-bound F4 or orchestration failure receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_failure_receipts import CAUSE_CLASS
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_lineage import load_complete_lineage

if TYPE_CHECKING:
    from pathlib import Path


def publish_final_wave_failure(ledger_path: Path, journal_path: Path) -> Path:
    """Publish one fixed common receipt after final-wave cleanup and sealing."""
    journal, journal_raw = load_json(journal_path)
    form = str(journal["form"])
    lane, stage = _lane_stage(form)
    cause = "io-failure"
    attempt_root = journal_path.parent
    root = attempt_root / "final-failure-receipts"
    root.mkdir(mode=MODE_DIRECTORY, exist_ok=True)
    lineage = load_complete_lineage(attempt_root, str(journal["attempt_id"]))
    with locked_open_ledger(ledger_path) as session:
        receipt: JsonObject = {
            "attempt_id": session.ledger["attempt_id"],
            "cause_code": cause,
            "cleanup_verified": True,
            "control_tree_sha256": None,
            "ended_at_utc": journal["updated_at_utc"],
            "exit_code": journal["wait_exit_code"],
            "failure_class": CAUSE_CLASS[cause],
            "journal_sha256": raw_sha256(journal_raw),
            "lane": lane,
            "lineage_validation_sha256": lineage.validation_sha256,
            "observed_causes": [cause],
            "output_sha256": journal["typed_outcome_sha256"],
            "schema_version": 1,
            "sha": session.ledger["foundation_sha"],
            "signal": journal["wait_signal"],
            "stage": stage,
            "started_at_utc": journal["started_at_utc"] or journal["updated_at_utc"],
            "timed_out": journal["timed_out"],
        }
        destination = root / f"{lane}.json"
        raw = canonical_bytes(receipt)
        if destination.exists():
            if destination.read_bytes() != raw:
                message = "final-wave failure receipt replay drifted"
                raise RuntimeError(message)
        else:
            write_no_replace(destination, raw, mode=MODE_IMMUTABLE)
    return destination


def _lane_stage(form: str) -> tuple[str, str]:
    if form == "scope-pre":
        return "F4", "F4-precheck"
    if form == "final":
        return "F4", "F4-final"
    if form == "inputs":
        return "ORCH", "ORCH-input-freeze"
    return "ORCH", "ORCH-pre-f4-freeze"
