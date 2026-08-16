"""Finish receipt-driven stale recovery without publishing a resume proof."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import IsolationError, JsonObject, raw_sha256
from ops.testing.isolation_failure_receipts import load_failure_receipts
from ops.testing.isolation_stale_journal import replace_recovery_journal
from ops.testing.isolation_stale_reject_records import (
    bind_finalized_receipts,
    bind_publisher_release_intent,
    bind_publisher_released,
    build_rejected_ledger,
    mark_receipts_finalizing,
    mark_reject_boot_updated,
    mark_reject_complete,
)
from ops.testing.isolation_terminal_publisher_release import (
    finish_publisher_rejection_release,
    prepare_publisher_rejection_release,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_ledger_store import LedgerSession

type Checkpoint = Callable[[str, JsonObject], None]


def finish_reject(
    session: LedgerSession,
    journal_path: Path,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    """Finalize receipts, bind updated bytes, and complete rejection replay."""
    if journal.get("recovery_goal") != "reject":
        _fail("resume recovery cannot enter the reject branch")
    journal = _enter_receipt_finalization(journal_path, journal, emit)
    journal = _finalize_receipts(session, journal_path, journal, emit)
    journal = _prepare_publisher_release(session, journal_path, journal, emit)
    journal = _update_reject_ledger(session, journal_path, journal, emit)
    journal = _finish_publisher_release(journal_path, journal, emit)
    if journal.get("state") in {"boot-updated", "publisher-released"}:
        journal = mark_reject_complete(journal, session.original_raw)
        replace_recovery_journal(journal_path, journal)
        emit("reject-complete", journal)
    if journal.get("state") != "complete":
        _fail("rejection recovery did not reach complete")
    if raw_sha256(session.original_raw) != journal.get("post_update_ledger_sha256"):
        _fail("complete rejection ledger differs from bound current-boot bytes")
    return journal


def _enter_receipt_finalization(
    path: Path,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    if journal.get("state") != "claims-pruned":
        return journal
    result = mark_receipts_finalizing(journal)
    replace_recovery_journal(path, result)
    emit("receipts-finalizing", result)
    return result


def _finalize_receipts(
    session: LedgerSession,
    path: Path,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    if journal.get("state") != "receipts-finalizing":
        return journal
    _require_finalized_controllers(journal)
    _receipts, aggregate = load_failure_receipts(session.ledger)
    if aggregate is None:
        _fail("rejection requires at least one common failure receipt")
    _, updated_raw = build_rejected_ledger(session.ledger, journal)
    result = bind_finalized_receipts(journal, aggregate, updated_raw)
    replace_recovery_journal(path, result)
    emit("receipts-finalized", result)
    return result


def _update_reject_ledger(
    session: LedgerSession,
    path: Path,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    expected = (
        "publisher-release-intent"
        if journal.get("publisher_claim_id") is not None
        else "receipts-finalized"
    )
    if journal.get("state") != expected:
        return journal
    _receipts, aggregate = load_failure_receipts(session.ledger)
    if aggregate != journal.get("failure_receipts_sha256"):
        _fail("failure receipt aggregate changed after finalization")
    updated, updated_raw = build_rejected_ledger(session.ledger, journal)
    if raw_sha256(updated_raw) != journal.get("post_update_ledger_sha256"):
        _fail("replayed rejection ledger differs from its prospective hash")
    current_hash = raw_sha256(session.original_raw)
    if current_hash == journal.get("post_cleanup_ledger_sha256"):
        session.ledger.clear()
        session.ledger.update(updated)
        session.commit()
    elif current_hash != journal.get("post_update_ledger_sha256"):
        _fail("rejection ledger is neither pre-update nor bound post-update bytes")
    emit("reject-ledger-updated", journal)
    result = mark_reject_boot_updated(journal, session.original_raw)
    replace_recovery_journal(path, result)
    emit("reject-boot-updated", result)
    return result


def _prepare_publisher_release(
    session: LedgerSession,
    path: Path,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    if journal.get("state") != "receipts-finalized":
        return journal
    if journal.get("publisher_claim_id") is None:
        return journal
    intent_sha256 = prepare_publisher_rejection_release(session.ledger, journal, emit)
    result = bind_publisher_release_intent(journal, intent_sha256)
    replace_recovery_journal(path, result)
    emit("publisher-release-intent", result)
    return result


def _finish_publisher_release(
    path: Path,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    if journal.get("state") != "boot-updated":
        return journal
    if journal.get("publisher_claim_id") is None:
        return journal
    final_sha256 = finish_publisher_rejection_release(journal)
    result = bind_publisher_released(journal, final_sha256)
    replace_recovery_journal(path, result)
    emit("publisher-released", result)
    return result


def _require_finalized_controllers(journal: JsonObject) -> None:
    if journal.get("f3_recovery_required") is True:
        _fail("F3 receipt finalization adapter is not complete")
    recoveries = journal.get("controller_recoveries")
    if not isinstance(recoveries, list):
        _fail("controller recoveries are not an array")
    if recoveries:
        _fail("controller receipt finalization adapter is not complete")


def _fail(message: str) -> Never:
    raise IsolationError(message)
