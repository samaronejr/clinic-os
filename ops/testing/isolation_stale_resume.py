"""Finish a quiescent resume branch through proof-bound boot rebind."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import IsolationError, JsonObject, raw_sha256
from ops.testing.isolation_stale_journal import replace_recovery_journal
from ops.testing.isolation_stale_resume_records import (
    bind_resume_proof,
    build_resumed_ledger,
    mark_resume_boot_updated,
    mark_resume_complete,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_ledger_store import LedgerSession

type Checkpoint = Callable[[str, JsonObject], None]
type ProofPublisher = Callable[[LedgerSession, JsonObject], JsonObject]


def finish_resume(
    session: LedgerSession,
    journal_path: Path,
    journal: JsonObject,
    emit: Checkpoint,
    publisher: ProofPublisher,
) -> JsonObject:
    """Publish proof, bind updated bytes, replace the ledger, and acknowledge."""
    if journal.get("recovery_goal") != "resume":
        _fail("reject recovery cannot enter the resume branch")
    if journal.get("state") == "claims-pruned":
        proof = publisher(session, journal)
        _, updated_raw = build_resumed_ledger(session.ledger, journal, proof)
        journal = bind_resume_proof(journal, proof, updated_raw)
        replace_recovery_journal(journal_path, journal)
        emit("resume-proof-published", journal)
    if journal.get("state") == "resume-proof-published":
        proof = publisher(session, journal)
        _require_bound_proof(journal, proof)
        updated, updated_raw = build_resumed_ledger(session.ledger, journal, proof)
        if raw_sha256(updated_raw) != journal.get("post_update_ledger_sha256"):
            _fail("replayed resume ledger differs from its prospective hash")
        current_hash = raw_sha256(session.original_raw)
        if current_hash == journal.get("post_cleanup_ledger_sha256"):
            session.ledger.clear()
            session.ledger.update(updated)
            session.commit()
        elif current_hash != journal.get("post_update_ledger_sha256"):
            _fail("resume ledger is neither pre-update nor bound post-update bytes")
        emit("resume-ledger-updated", journal)
        journal = mark_resume_boot_updated(journal, session.original_raw)
        replace_recovery_journal(journal_path, journal)
        emit("resume-boot-updated", journal)
    if journal.get("state") == "boot-updated":
        journal = mark_resume_complete(journal, session.original_raw)
        replace_recovery_journal(journal_path, journal)
        emit("resume-complete", journal)
    if journal.get("state") != "complete":
        _fail("resume recovery did not reach complete")
    if raw_sha256(session.original_raw) != journal.get("post_update_ledger_sha256"):
        _fail("complete resume ledger differs from bound current-boot bytes")
    return journal


def _require_bound_proof(journal: JsonObject, proof: JsonObject) -> None:
    if journal.get("resume_execution_host_preflight_path") != proof.get(
        "path"
    ) or journal.get("resume_execution_host_preflight_sha256") != proof.get("sha256"):
        _fail("replayed resume proof differs from journal authority")


def _fail(message: str) -> Never:
    raise IsolationError(message)
