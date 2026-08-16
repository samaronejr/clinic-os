"""Authenticate one complete rejection bundle for successor publication."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_archive_contract import ArchiveRequest, archive_paths
from ops.testing.isolation_archive_state import load_archive_state
from ops.testing.isolation_archive_validation import (
    validate_successor_archive_evidence,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
    regular_identity,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidatedRetryArchive:
    """Immutable predecessor evidence used to derive one successor attempt."""

    bundle: Path
    ledger: JsonObject
    tombstone: JsonObject
    tombstone_raw: bytes
    successor_attempt_id: str


def validate_retry_archive(
    ledger_path: Path,
    closed_attempt: str,
    lock_identity: JsonObject,
    inventory_reader: Callable[[], JsonObject],
) -> ValidatedRetryArchive:
    """Validate the sole complete bundle without repairing an archive prefix."""
    bundle, tombstone, tombstone_raw = _discover_bundle(
        ledger_path,
        closed_attempt,
    )
    sha = tombstone.get("sha")
    specification_sha = tombstone.get("rejection_spec_sha256")
    if not isinstance(sha, str) or not isinstance(specification_sha, str):
        _fail("archive tombstone lacks retry selectors")
    request = ArchiveRequest(
        closed_attempt=closed_attempt,
        rejected_sha=sha,
        control_root=ledger_path.parent / "clinic-os-phase1a-final",
        rejection_spec_sha256=specification_sha,
    )
    paths = archive_paths(ledger_path, request)
    loaded = load_archive_state(paths, request, lock_identity)
    if (
        loaded is None
        or loaded.journal.get("state") != "complete"
        or loaded.sentinel_present
    ):
        _fail("archive is not one complete sentinel-free bundle")
    evidence = validate_successor_archive_evidence(
        paths,
        request,
        lock_identity,
        inventory_reader,
        loaded.journal,
    )
    if evidence.tombstone_raw != tombstone_raw or loaded.journal.get(
        "tombstone_sha256"
    ) != raw_sha256(tombstone_raw):
        _fail("archive tombstone differs from complete journal evidence")
    return ValidatedRetryArchive(
        bundle,
        evidence.ledger,
        tombstone,
        tombstone_raw,
        _successor_attempt_id(closed_attempt, tombstone_raw),
    )


def _successor_attempt_id(closed_attempt: str, tombstone_raw: bytes) -> str:
    return str(uuid.uuid5(uuid.UUID(closed_attempt), raw_sha256(tombstone_raw)))


def _discover_bundle(
    ledger_path: Path,
    closed_attempt: str,
) -> tuple[Path, JsonObject, bytes]:
    if ledger_path.is_symlink():
        _fail("canonical successor ledger path is a symlink")
    attempt_history = ledger_path.parent / "clinic-os-phase1a-rejected" / closed_attempt
    if attempt_history.is_symlink() or not attempt_history.is_dir():
        _fail("archive attempt history is absent or noncanonical")
    children = tuple(attempt_history.iterdir())
    if len(children) != 1:
        _fail("archive attempt history must contain exactly one bundle")
    bundle = children[0]
    if bundle.is_symlink() or not bundle.is_dir():
        _fail("archive bundle is not a canonical directory")
    tombstone_path = bundle / "rejection.json"
    regular_identity(tombstone_path, mode=MODE_IMMUTABLE)
    tombstone, raw = load_json(tombstone_path)
    return bundle, tombstone, raw


def _fail(message: str) -> Never:
    raise IsolationError(message)
