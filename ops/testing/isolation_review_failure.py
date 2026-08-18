"""Publish one journal-bound common failure receipt for F1 or F2."""

from __future__ import annotations

from pathlib import Path
from typing import Never

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_failure_receipts import CAUSE_CLASS
from ops.testing.isolation_ledger_store import locked_open_ledger


def publish_review_failure(
    ledger_path: Path,
    journal_path: Path,
    lane: str,
    cause: str,
    lineage_sha256: str,
) -> Path:
    """Write the fixed immutable receipt after cleanup and journal sealing."""
    journal, journal_raw = load_json(journal_path)
    if journal.get("state") != "failure-ready":
        _fail("review failure journal is not sealed failure-ready")
    stage = "F1-review" if lane == "F1" else _f2_stage(journal.get("stage"))
    root = Path(str(journal_path.parents[1])) / "final-failure-receipts"
    root.mkdir(mode=MODE_DIRECTORY, exist_ok=True)
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
            "lineage_validation_sha256": lineage_sha256,
            "observed_causes": [cause],
            "output_sha256": journal["typed_outcome_sha256"],
            "schema_version": 1,
            "sha": session.ledger["foundation_sha"],
            "signal": journal["wait_signal"],
            "stage": stage,
            "started_at_utc": journal["started_at_utc"] or journal["updated_at_utc"],
            "timed_out": journal["timed_out"],
        }
        raw = canonical_bytes(receipt)
        destination = root / f"{lane}.json"
        if destination.exists():
            if destination.read_bytes() != raw:
                _fail("review failure receipt replay drifted")
        else:
            write_no_replace(destination, raw, mode=MODE_IMMUTABLE)
    return destination


def _f2_stage(value: object) -> str:
    return "F2-review" if value == "review" else "F2-prerequisites"


def _fail(message: str) -> Never:
    raise IsolationError(message)
