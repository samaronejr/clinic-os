"""Merkle-inventory unrelated shared evidence without following symlinks."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
)

if TYPE_CHECKING:
    from pathlib import Path


FIXED_EXCLUSIONS: Final = (
    "clinic-os-phase1a-final",
    "isolation-archive-rollover-phase1a.json",
    "isolation-ledger-final-phase1a.json",
    "isolation-ledger-phase1a.json",
    "isolation-ledger-phase1a.lock",
    "review-inputs/approved-plan.md",
    "review-inputs/approved-plan.sha256",
)
ARCHIVE_SENTINEL: Final = "isolation-archive-rollover-phase1a.sentinel"


@dataclass(frozen=True, slots=True)
class ManifestAllowances:
    """Authenticated transient paths allowed during one manifest comparison."""

    archive_sentinel: bool = False
    successor_attempt_id: str | None = None


DEFAULT_ALLOWANCES: Final = ManifestAllowances()


def capture_manifest(
    evidence_root: Path,
    *,
    attempt_id: str,
    allowances: ManifestAllowances = DEFAULT_ALLOWANCES,
) -> JsonObject:
    """Capture immutable identities for every unrelated evidence entry."""
    exclusions = _phase1a_exclusions(
        attempt_id,
        allowances,
    )
    root_identity = evidence_root.lstat()
    if (
        not stat.S_ISDIR(root_identity.st_mode)
        or root_identity.st_uid != os.geteuid()
        or root_identity.st_gid != os.getegid()
    ):
        message = "evidence root must be an executor-owned directory"
        raise IsolationError(message)
    entries: list[JsonValue] = []
    children = sorted(
        evidence_root.iterdir(),
        key=lambda path: os.fsencode(path.name),
    )
    for child in children:
        entries.extend(_walk_entry(evidence_root, child, exclusions))
    manifest: JsonObject = {
        "entries": entries,
        "entry_count": len(entries),
        "evidence_root": str(evidence_root.resolve(strict=True)),
        "schema_version": 1,
    }
    manifest["entries_sha256"] = hashlib.sha256(canonical_bytes(entries)).hexdigest()
    return manifest


def verify_manifest(
    evidence_root: Path,
    expected: JsonObject,
    *,
    attempt_id: str,
    allowances: ManifestAllowances = DEFAULT_ALLOWANCES,
) -> None:
    """Fail when any unrelated shared-evidence inode or byte changes."""
    observed = capture_manifest(
        evidence_root,
        attempt_id=attempt_id,
        allowances=allowances,
    )
    if observed != expected:
        message = "unrelated shared-evidence baseline drifted"
        raise IsolationError(message)


def _walk_entry(
    root: Path,
    path: Path,
    exclusions: tuple[str, ...],
) -> list[JsonValue]:
    relative = path.relative_to(root).as_posix()
    if _is_excluded(relative, exclusions):
        return []
    value = path.lstat()
    common: JsonObject = {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "mode": stat.S_IMODE(value.st_mode),
        "relative_path": relative,
        "uid": value.st_uid,
    }
    if stat.S_ISREG(value.st_mode):
        common.update(
            {
                "link_count": value.st_nlink,
                "sha256": _hash_regular(path, value),
                "size_bytes": value.st_size,
                "type": "regular",
            }
        )
        return [common]
    if stat.S_ISLNK(value.st_mode):
        common.update(
            {
                "link_count": value.st_nlink,
                "link_target": str(path.readlink()),
                "type": "symlink",
            }
        )
        return [common]
    if stat.S_ISDIR(value.st_mode):
        common["type"] = "directory"
        descendants: list[JsonValue] = []
        for child in sorted(path.iterdir(), key=lambda item: os.fsencode(item.name)):
            descendants.extend(_walk_entry(root, child, exclusions))
        if not descendants and _is_exclusion_ancestor(relative, exclusions):
            return []
        return [common, *descendants]
    message = f"unsupported shared-evidence entry type: {relative}"
    raise IsolationError(message)


def _phase1a_exclusions(
    attempt_id: str,
    allowances: ManifestAllowances,
) -> tuple[str, ...]:
    _validate_attempt_id(attempt_id)
    transient = (ARCHIVE_SENTINEL,) if allowances.archive_sentinel else ()
    successor: tuple[str, ...] = ()
    if allowances.successor_attempt_id is not None:
        _validate_attempt_id(allowances.successor_attempt_id)
        successor = (f"clinic-os-phase1a-runtime/{allowances.successor_attempt_id}",)
    return tuple(
        sorted(
            (
                *FIXED_EXCLUSIONS,
                *transient,
                *successor,
                f"clinic-os-phase1a-rejected/{attempt_id}",
                f"clinic-os-phase1a-runtime/{attempt_id}",
            )
        )
    )


def _validate_attempt_id(attempt_id: str) -> None:
    try:
        parsed = uuid.UUID(attempt_id)
    except ValueError as error:
        message = "attempt ID must be a canonical UUID"
        raise IsolationError(message) from error
    if str(parsed) != attempt_id:
        message = "attempt ID must be a canonical UUID"
        raise IsolationError(message)


def _is_excluded(relative: str, exclusions: tuple[str, ...]) -> bool:
    return any(
        relative == excluded or relative.startswith(f"{excluded}/")
        for excluded in exclusions
    )


def _is_exclusion_ancestor(relative: str, exclusions: tuple[str, ...]) -> bool:
    return any(excluded.startswith(f"{relative}/") for excluded in exclusions)


def _hash_regular(path: Path, expected: os.stat_result) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        _require_same_identity(os.fstat(descriptor), expected)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        _require_same_identity(os.fstat(descriptor), expected)
        _require_same_identity(path.lstat(), expected)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _require_same_identity(observed: os.stat_result, expected: os.stat_result) -> None:
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
    )
    if any(getattr(observed, field) != getattr(expected, field) for field in fields):
        message = "shared-evidence entry changed during inventory"
        raise IsolationError(message)
