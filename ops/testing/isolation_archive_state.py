"""Classify and persist the stable-versus-bundled archive journal state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_archive_records import validate_journal, validate_sentinel
from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    load_json,
    regular_identity,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_archive_contract import ArchivePaths, ArchiveRequest


@dataclass(frozen=True, slots=True)
class LoadedArchiveState:
    """One authenticated journal location plus its stable-sentinel presence."""

    journal: JsonObject
    path: Path
    sentinel_present: bool


def load_archive_state(
    paths: ArchivePaths,
    request: ArchiveRequest,
    lock_identity: JsonObject,
) -> LoadedArchiveState | None:
    """Recognize only legal stable, relocating, complete, or fresh prefixes."""
    stable_present = path_present(paths.journal)
    bundled_present = path_present(paths.bundled_journal)
    sentinel_present = path_present(paths.sentinel)
    if stable_present and bundled_present:
        _fail("archive journal exists at both stable and bundled locations")
    if not stable_present and not bundled_present:
        if sentinel_present:
            _fail("archive sentinel exists without an authenticated journal")
        return None
    path = paths.journal if stable_present else paths.bundled_journal
    regular_identity(path, mode=MODE_PRIVATE)
    journal, _raw = load_json(path)
    validate_journal(journal, paths, request, lock_identity)
    state = journal.get("state")
    if stable_present and state == "complete":
        _fail("complete archive journal remained at the stable location")
    if bundled_present and state not in {"relocating", "complete"}:
        _fail("nonrelocating archive journal appeared inside the bundle")
    if sentinel_present:
        regular_identity(paths.sentinel, mode=MODE_PRIVATE)
        sentinel, _raw = load_json(paths.sentinel)
        validate_sentinel(sentinel, journal)
    elif state != "prepared" and not (bundled_present and state == "complete"):
        _fail("archive journal state requires its stable sentinel")
    return LoadedArchiveState(journal, path, sentinel_present)


def path_present(path: Path) -> bool:
    """Classify both existing entries and dangling symlinks as occupied."""
    return path.exists() or path.is_symlink()


def _fail(message: str) -> Never:
    raise IsolationError(message)
