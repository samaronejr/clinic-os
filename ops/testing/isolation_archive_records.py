"""Build and validate archive journal and sentinel records."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Final, Never

from ops.testing.isolation_archive_contract import (
    SHA256_PATTERN,
    ArchivePaths,
    ArchiveRequest,
)
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_sha256,
    utc_now,
)

JOURNAL_KEYS: Final = {
    "schema_version",
    "attempt_id",
    "rejected_sha",
    "rejection_spec_sha256",
    "bundle_path",
    "stable_lock_identity_sha256",
    "archive_identity_sha256",
    "state",
    "control_tree_sha256",
    "attempt_tree_sha256",
    "ledger_sha256",
    "tombstone_sha256",
    "updated_at_utc",
}
SENTINEL_KEYS: Final = {
    "schema_version",
    "attempt_id",
    "rejected_sha",
    "bundle_path",
    "archive_identity_sha256",
    "stable_lock_identity_sha256",
}
STATES: Final = (
    "prepared",
    "control-moved",
    "attempt-moved",
    "ledger-moved",
    "tombstone-published",
    "relocating",
    "complete",
)


@dataclass(frozen=True, slots=True)
class ArchiveSourceHashes:
    """Prepared hashes for control, attempt, and closed ledger sources."""

    control_tree_sha256: str
    attempt_tree_sha256: str
    ledger_sha256: str


def prepared_journal(
    paths: ArchivePaths,
    request: ArchiveRequest,
    lock_identity: JsonObject,
    source_hashes: ArchiveSourceHashes,
) -> JsonObject:
    """Bind every source and destination before the first archive rename."""
    lock_sha = canonical_sha256(lock_identity)
    identity: JsonObject = {
        "attempt_id": request.closed_attempt,
        "bundle_path": str(paths.bundle),
        "control_tree_sha256": source_hashes.control_tree_sha256,
        "attempt_tree_sha256": source_hashes.attempt_tree_sha256,
        "ledger_sha256": source_hashes.ledger_sha256,
        "rejected_sha": request.rejected_sha,
        "rejection_spec_sha256": request.rejection_spec_sha256,
        "schema_version": 1,
        "stable_lock_identity_sha256": lock_sha,
    }
    return {
        **identity,
        "archive_identity_sha256": canonical_sha256(identity),
        "state": "prepared",
        "tombstone_sha256": None,
        "updated_at_utc": utc_now(),
    }


def sentinel_record(journal: JsonObject) -> JsonObject:
    """Project the immutable stable sentinel from its prepared journal."""
    return {
        "archive_identity_sha256": journal["archive_identity_sha256"],
        "attempt_id": journal["attempt_id"],
        "bundle_path": journal["bundle_path"],
        "rejected_sha": journal["rejected_sha"],
        "schema_version": 1,
        "stable_lock_identity_sha256": journal["stable_lock_identity_sha256"],
    }


def advance_journal(
    journal: JsonObject,
    state: str,
    *,
    tombstone_sha256: str | None = None,
) -> JsonObject:
    """Advance exactly one ordered state while preserving archive identity."""
    current = journal.get("state")
    if current not in STATES or state not in STATES:
        _fail("archive journal state is unknown")
    if STATES.index(state) != STATES.index(str(current)) + 1:
        _fail("archive journal state transition is not adjacent")
    result = copy.deepcopy(journal)
    result["state"] = state
    result["updated_at_utc"] = utc_now()
    if state == "tombstone-published":
        if tombstone_sha256 is None:
            _fail("tombstone publication lacks its hash")
        result["tombstone_sha256"] = tombstone_sha256
    elif tombstone_sha256 is not None:
        _fail("tombstone hash may change only at publication")
    return result


def validate_journal(
    journal: JsonObject,
    paths: ArchivePaths,
    request: ArchiveRequest,
    lock_identity: JsonObject,
) -> None:
    """Authenticate the exact mutable state around its immutable identity."""
    if set(journal) != JOURNAL_KEYS or journal.get("schema_version") != 1:
        _fail("archive journal has an open or unknown root")
    if (
        journal.get("attempt_id") != request.closed_attempt
        or journal.get("rejected_sha") != request.rejected_sha
        or journal.get("rejection_spec_sha256") != request.rejection_spec_sha256
        or journal.get("bundle_path") != str(paths.bundle)
    ):
        _fail("archive journal differs from requested authority")
    if journal.get("stable_lock_identity_sha256") != canonical_sha256(lock_identity):
        _fail("archive journal stable-lock identity changed")
    state = journal.get("state")
    if state not in STATES:
        _fail("archive journal state is invalid")
    for key in ("control_tree_sha256", "attempt_tree_sha256", "ledger_sha256"):
        if not _sha(journal.get(key)):
            _fail(f"archive journal {key} is invalid")
    tombstone = journal.get("tombstone_sha256")
    if (STATES.index(str(state)) < STATES.index("tombstone-published")) != (
        tombstone is None
    ):
        _fail("archive journal tombstone hash disagrees with state")
    if tombstone is not None and not _sha(tombstone):
        _fail("archive journal tombstone hash is invalid")
    identity = {
        key: journal[key]
        for key in (
            "attempt_id",
            "bundle_path",
            "control_tree_sha256",
            "attempt_tree_sha256",
            "ledger_sha256",
            "rejected_sha",
            "rejection_spec_sha256",
            "schema_version",
            "stable_lock_identity_sha256",
        )
    }
    if journal.get("archive_identity_sha256") != canonical_sha256(identity):
        _fail("archive journal immutable identity changed")


def validate_sentinel(sentinel: JsonObject, journal: JsonObject) -> None:
    """Require the stable sentinel to equal its journal projection."""
    if set(sentinel) != SENTINEL_KEYS or sentinel != sentinel_record(journal):
        _fail("archive sentinel differs from its journal authority")


def _sha(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _fail(message: str) -> Never:
    raise IsolationError(message)
