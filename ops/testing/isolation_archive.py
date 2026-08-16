"""Crash-safely relocate one closed rejected attempt into immutable history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.isolation_archive_contract import (
    ArchivePaths,
    ArchiveRequest,
    archive_paths,
)
from ops.testing.isolation_archive_records import (
    ArchiveSourceHashes,
    prepared_journal,
    sentinel_record,
)
from ops.testing.isolation_archive_state import (
    LoadedArchiveState,
    load_archive_state,
    path_present,
)
from ops.testing.isolation_archive_transitions import (
    complete_journal,
    publish_tombstone,
    relocate_journal,
    relocate_sources,
    validate_or_publish_tombstone,
)
from ops.testing.isolation_archive_tree import ensure_bundle_directories
from ops.testing.isolation_archive_validation import (
    ArchiveEvidence,
    validate_archive_evidence,
)
from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    fsync_directory,
    raw_sha256,
    stable_lock,
    write_no_replace,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    type Checkpoint = Callable[[str, JsonObject], None]

__all__ = ("ArchiveRequest", "archive_rollover")


def archive_rollover(
    ledger_path: Path,
    request: ArchiveRequest,
    *,
    inventory_reader: Callable[[], JsonObject],
    checkpoint: Callable[[str, JsonObject], None] | None = None,
) -> Path:
    """Relocate or replay the exact journal-bound rejected-attempt bundle."""
    paths = archive_paths(ledger_path, request)
    emit = checkpoint if checkpoint is not None else _no_checkpoint
    with stable_lock(paths.lock, create=False) as (_descriptor, lock_identity):
        loaded = load_archive_state(paths, request, lock_identity)
        if loaded is None:
            if path_present(paths.bundle):
                _fail("archive bundle destination already exists")
            evidence = validate_archive_evidence(
                paths,
                request,
                lock_identity,
                inventory_reader,
            )
            journal = _prepare(paths, request, lock_identity, evidence)
            emit("prepared", journal)
            loaded = LoadedArchiveState(
                journal=journal,
                path=paths.journal,
                sentinel_present=False,
            )
        else:
            evidence = validate_archive_evidence(
                paths,
                request,
                lock_identity,
                inventory_reader,
                loaded.journal,
            )
        journal = loaded.journal
        if journal.get("state") == "complete":
            _finish_complete(
                paths,
                evidence,
                journal,
                sentinel_present=loaded.sentinel_present,
            )
            return paths.bundle
        if not loaded.sentinel_present:
            _publish_sentinel(paths, journal)
            emit("sentinel-created", journal)
        ensure_bundle_directories(paths.bundle)
        journal = relocate_sources(paths, evidence, journal, emit)
        journal = publish_tombstone(paths, evidence, journal, emit)
        journal = relocate_journal(paths, journal, emit)
        journal = complete_journal(paths, journal, emit)
        final_evidence = validate_archive_evidence(
            paths,
            request,
            lock_identity,
            inventory_reader,
            journal,
        )
        _finish_complete(paths, final_evidence, journal, sentinel_present=True)
    return paths.bundle


def _prepare(
    paths: ArchivePaths,
    request: ArchiveRequest,
    lock_identity: JsonObject,
    evidence: ArchiveEvidence,
) -> JsonObject:
    journal = prepared_journal(
        paths,
        request,
        lock_identity,
        ArchiveSourceHashes(
            control_tree_sha256=evidence.control_tree_sha256,
            attempt_tree_sha256=evidence.attempt_tree_sha256,
            ledger_sha256=evidence.ledger_sha256,
        ),
    )
    write_no_replace(paths.journal, _raw(journal), mode=MODE_PRIVATE)
    return journal


def _publish_sentinel(paths: ArchivePaths, journal: JsonObject) -> None:
    if journal.get("state") != "prepared":
        _fail("only a prepared archive may publish its sentinel")
    write_no_replace(
        paths.sentinel,
        _raw(sentinel_record(journal)),
        mode=MODE_PRIVATE,
    )


def _finish_complete(
    paths: ArchivePaths,
    evidence: ArchiveEvidence,
    journal: JsonObject,
    *,
    sentinel_present: bool,
) -> None:
    if journal.get("state") != "complete":
        _fail("archive journal did not reach complete")
    if any(path_present(path) for path in (paths.control, paths.attempt, paths.ledger)):
        _fail("complete archive retained a canonical source")
    validate_or_publish_tombstone(paths, evidence)
    if raw_sha256(evidence.tombstone_raw) != journal.get("tombstone_sha256"):
        _fail("complete archive tombstone differs from journal")
    if sentinel_present:
        paths.sentinel.unlink()
        fsync_directory(paths.sentinel.parent)


def _raw(value: JsonObject) -> bytes:
    return canonical_bytes(value)


def _no_checkpoint(_stage: str, _journal: JsonObject) -> None:
    return


def _fail(message: str) -> Never:
    raise IsolationError(message)
