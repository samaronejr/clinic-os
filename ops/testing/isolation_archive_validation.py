"""Revalidate archive sources or relocated bundle entries under one lock."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_archive_ledger import validate_closed_archive_ledger
from ops.testing.isolation_archive_tombstone import (
    ArchiveContentHashes,
    build_archive_tombstone,
)
from ops.testing.isolation_archive_tree import tree_sha256
from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_quiescence import require_quiescent_attempt
from ops.testing.isolation_snapshot_records import ROOT_KEYS
from ops.testing.shared_evidence_baseline import ManifestAllowances

SHA256_LENGTH: Final = 64

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_archive_contract import ArchivePaths, ArchiveRequest


@dataclass(frozen=True, slots=True)
class ArchiveEvidence:
    """Validated logical entries and hashes bound by archive preparation."""

    ledger: JsonObject
    control_path: Path
    attempt_path: Path
    ledger_path: Path
    control_tree_sha256: str
    attempt_tree_sha256: str
    ledger_sha256: str
    tombstone: JsonObject
    tombstone_raw: bytes


@dataclass(frozen=True, slots=True)
class _ArchiveValidationOptions:
    journal: JsonObject | None
    ignore_canonical_ledger: bool
    successor_attempt_id: str | None


def validate_archive_evidence(
    paths: ArchivePaths,
    request: ArchiveRequest,
    lock_identity: JsonObject,
    inventory_reader: Callable[[], JsonObject],
    journal: JsonObject | None = None,
) -> ArchiveEvidence:
    """Validate one exact source-or-bundle locator for every bound entry."""
    return _validate_archive_evidence(
        paths,
        request,
        lock_identity,
        inventory_reader,
        _ArchiveValidationOptions(
            journal=journal,
            ignore_canonical_ledger=False,
            successor_attempt_id=None,
        ),
    )


def validate_successor_archive_evidence(
    paths: ArchivePaths,
    request: ArchiveRequest,
    lock_identity: JsonObject,
    inventory_reader: Callable[[], JsonObject],
    journal: JsonObject,
) -> ArchiveEvidence:
    """Validate archived sources while a successor ledger may already exist."""
    return _validate_archive_evidence(
        paths,
        request,
        lock_identity,
        inventory_reader,
        _ArchiveValidationOptions(
            journal=journal,
            ignore_canonical_ledger=True,
            successor_attempt_id=_successor_attempt_id(request, journal),
        ),
    )


def _successor_attempt_id(
    request: ArchiveRequest,
    journal: JsonObject,
) -> str:
    digest = journal.get("tombstone_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        _fail("complete archive journal lacks its tombstone hash")
    try:
        namespace = uuid.UUID(request.closed_attempt)
    except ValueError as error:
        message = "closed attempt ID is not a UUID"
        raise IsolationError(message) from error
    return str(uuid.uuid5(namespace, digest))


def _validate_archive_evidence(
    paths: ArchivePaths,
    request: ArchiveRequest,
    lock_identity: JsonObject,
    inventory_reader: Callable[[], JsonObject],
    options: _ArchiveValidationOptions,
) -> ArchiveEvidence:
    control_path = _one_locator(paths.control, paths.bundled_control, "control")
    attempt_path = _one_locator(paths.attempt, paths.bundled_attempt, "attempt")
    ledger_path = (
        _required_bundled_ledger(paths.bundled_ledger)
        if options.ignore_canonical_ledger
        else _one_locator(paths.ledger, paths.bundled_ledger, "ledger")
    )
    regular_identity(ledger_path, mode=MODE_PRIVATE)
    ledger, ledger_raw = load_json(ledger_path)
    validate_closed_archive_ledger(
        paths,
        request,
        ledger,
        lock_identity,
        ROOT_KEYS,
    )
    require_quiescent_attempt(
        paths.ledger,
        ledger,
        inventory_reader,
        attempt_root_override=attempt_path,
        manifest_allowances=ManifestAllowances(
            archive_sentinel=options.journal is not None,
            successor_attempt_id=options.successor_attempt_id,
        ),
    )
    control_sha = tree_sha256(control_path)
    attempt_sha = tree_sha256(attempt_path)
    ledger_sha = raw_sha256(ledger_raw)
    if options.journal is not None:
        _validate_journal_hashes(
            options.journal,
            control_sha,
            attempt_sha,
            ledger_sha,
        )
    tombstone, tombstone_raw = build_archive_tombstone(
        ledger,
        attempt_path,
        request,
        ArchiveContentHashes(
            control_sha256=control_sha,
            attempt_sha256=attempt_sha,
            ledger_sha256=ledger_sha,
        ),
    )
    return ArchiveEvidence(
        ledger=ledger,
        control_path=control_path,
        attempt_path=attempt_path,
        ledger_path=ledger_path,
        control_tree_sha256=control_sha,
        attempt_tree_sha256=attempt_sha,
        ledger_sha256=ledger_sha,
        tombstone=tombstone,
        tombstone_raw=tombstone_raw,
    )


def _validate_journal_hashes(
    journal: JsonObject,
    control_sha: str,
    attempt_sha: str,
    ledger_sha: str,
) -> None:
    expected = (control_sha, attempt_sha, ledger_sha)
    observed = tuple(
        journal.get(key)
        for key in (
            "control_tree_sha256",
            "attempt_tree_sha256",
            "ledger_sha256",
        )
    )
    if observed != expected:
        _fail("archive source hashes differ from prepared journal")


def _one_locator(source: Path, bundled: Path, label: str) -> Path:
    source_present = _present(source)
    bundled_present = _present(bundled)
    if source_present == bundled_present:
        _fail(f"archive {label} must exist at exactly one logical locator")
    return source if source_present else bundled


def _required_bundled_ledger(path: Path) -> Path:
    if not _present(path):
        _fail("archive bundled ledger is absent")
    return path


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _fail(message: str) -> Never:
    raise IsolationError(message)
