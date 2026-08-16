"""Apply idempotent archive moves and adjacent journal transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.isolation_archive_records import advance_journal
from ops.testing.isolation_archive_state import path_present
from ops.testing.isolation_archive_tree import move_no_replace, tree_sha256
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    write_atomic_replace,
    write_no_replace,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_archive_contract import ArchivePaths
    from ops.testing.isolation_archive_validation import ArchiveEvidence

    type Checkpoint = Callable[[str, JsonObject], None]


def relocate_sources(
    paths: ArchivePaths,
    evidence: ArchiveEvidence,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    """Move control, attempt, and ledger in their fixed journal order."""
    if journal.get("state") == "prepared":
        _ensure_tree_moved(
            paths.control,
            paths.bundled_control,
            evidence.control_tree_sha256,
        )
        journal = _advance_stable(paths, journal, "control-moved")
        emit("control-moved", journal)
    if journal.get("state") == "control-moved":
        _ensure_tree_moved(
            paths.attempt,
            paths.bundled_attempt,
            evidence.attempt_tree_sha256,
        )
        journal = _advance_stable(paths, journal, "attempt-moved")
        emit("attempt-moved", journal)
    if journal.get("state") == "attempt-moved":
        _ensure_file_moved(
            paths.ledger,
            paths.bundled_ledger,
            evidence.ledger_sha256,
        )
        journal = _advance_stable(paths, journal, "ledger-moved")
        emit("ledger-moved", journal)
    return journal


def publish_tombstone(
    paths: ArchivePaths,
    evidence: ArchiveEvidence,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    """Publish or adopt the exact tombstone before binding its journal hash."""
    if journal.get("state") != "ledger-moved":
        return journal
    validate_or_publish_tombstone(paths, evidence)
    digest = raw_sha256(evidence.tombstone_raw)
    result = advance_journal(
        journal,
        "tombstone-published",
        tombstone_sha256=digest,
    )
    write_atomic_replace(paths.journal, canonical_bytes(result))
    emit("tombstone-published", result)
    return result


def relocate_journal(
    paths: ArchivePaths,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    """Advance stable relocating state and no-replace move that journal."""
    state = journal.get("state")
    if state == "tombstone-published":
        journal = _advance_stable(paths, journal, "relocating")
        emit("relocating", journal)
        state = "relocating"
    if state == "relocating" and path_present(paths.journal):
        move_no_replace(paths.journal, paths.bundled_journal)
        emit("journal-relocated", journal)
    return journal


def complete_journal(
    paths: ArchivePaths,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    """Persist complete only after the relocating journal is solely bundled."""
    if journal.get("state") != "relocating":
        return journal
    if path_present(paths.journal) or not path_present(paths.bundled_journal):
        _fail("relocating journal is not solely inside the bundle")
    result = advance_journal(journal, "complete")
    write_atomic_replace(paths.bundled_journal, canonical_bytes(result))
    emit("complete", result)
    return result


def validate_or_publish_tombstone(
    paths: ArchivePaths,
    evidence: ArchiveEvidence,
) -> None:
    """Create once or byte-validate the immutable rejection tombstone."""
    try:
        write_no_replace(paths.tombstone, evidence.tombstone_raw, mode=MODE_IMMUTABLE)
    except FileExistsError:
        regular_identity(paths.tombstone, mode=MODE_IMMUTABLE)
        _value, raw = load_json(paths.tombstone)
        if raw != evidence.tombstone_raw:
            _fail("existing rejection tombstone differs from archive evidence")


def _advance_stable(
    paths: ArchivePaths,
    journal: JsonObject,
    state: str,
) -> JsonObject:
    result = advance_journal(journal, state)
    write_atomic_replace(paths.journal, canonical_bytes(result))
    return result


def _ensure_tree_moved(source: Path, destination: Path, digest: str) -> None:
    _ensure_moved(source, destination)
    if tree_sha256(destination) != digest:
        _fail("relocated archive tree differs from prepared hash")


def _ensure_file_moved(source: Path, destination: Path, digest: str) -> None:
    _ensure_moved(source, destination)
    regular_identity(destination, mode=MODE_PRIVATE)
    if raw_sha256(destination.read_bytes()) != digest:
        _fail("relocated archive ledger differs from prepared hash")


def _ensure_moved(source: Path, destination: Path) -> None:
    source_present = path_present(source)
    destination_present = path_present(destination)
    if source_present and not destination_present:
        move_no_replace(source, destination)
        return
    if source_present or not destination_present:
        _fail("archive relocation has an ambiguous source/destination prefix")


def _fail(message: str) -> Never:
    raise IsolationError(message)
