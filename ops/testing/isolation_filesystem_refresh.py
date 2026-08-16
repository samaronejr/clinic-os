"""Reobserve filesystem claims through no-follow directory descriptors."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Never, cast

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    IsolationError,
    JsonObject,
    JsonValue,
)
from ops.testing.isolation_filesystem_claim import load_current_filesystem_observation

type ReservationState = Literal["absent", "complete"]
type TreeEntry = tuple[str, str]

DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


def _fail(message: str) -> Never:
    raise IsolationError(message)


def classify_filesystem_reservation(
    claim: JsonObject,
    claim_root: Path,
) -> tuple[ReservationState, JsonObject | None]:
    """Classify only an exact empty or complete reserved filesystem root."""
    entries = _tree_entries(claim_root)
    if entries is None or entries == set():
        return "absent", None
    if entries != _expected_entries(claim):
        _fail("partial filesystem reservation has unexpected staging paths")
    try:
        observed = load_current_filesystem_observation(claim, claim_root)
    except (FileNotFoundError, IsolationError, OSError) as error:
        message = "partial filesystem reservation failed exact identity checks"
        raise IsolationError(message) from error
    return "complete", observed


def require_active_filesystem_unchanged(
    claim: JsonObject,
    claim_root: Path,
) -> None:
    """Require every active ordinary filesystem inode and byte hash unchanged."""
    if _tree_entries(claim_root) != _expected_entries(claim):
        _fail("active filesystem staging paths drifted")
    current = load_current_filesystem_observation(claim, claim_root)
    if current != claim.get("observed"):
        _fail("active filesystem observation drifted")


def remove_empty_claim_root(claim_root: Path) -> None:
    """Durably remove only an already-proven empty private claim directory."""
    entries = _tree_entries(claim_root)
    if entries is None:
        return
    if entries:
        _fail("claim root became nonempty before removal")
    claim_root.rmdir()
    descriptor = os.open(claim_root.parent, DIRECTORY_FLAGS)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_entries(root: Path) -> set[TreeEntry] | None:
    try:
        descriptor = os.open(root, DIRECTORY_FLAGS)
    except FileNotFoundError:
        return None
    try:
        identity = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(identity.st_mode)
            or stat.S_IMODE(identity.st_mode) != MODE_DIRECTORY
            or identity.st_uid != os.geteuid()
            or identity.st_gid != os.getegid()
        ):
            _fail("claim root is not the exact private executor directory")
        return _scan_tree(descriptor, PurePosixPath())
    finally:
        os.close(descriptor)


def _scan_tree(descriptor: int, parent: PurePosixPath) -> set[TreeEntry]:
    entries: set[TreeEntry] = set()
    with os.scandir(descriptor) as children:
        ordered = sorted(children, key=lambda item: item.name)
    for child in ordered:
        relative = parent / child.name
        identity = child.stat(follow_symlinks=False)
        if stat.S_ISREG(identity.st_mode):
            entries.add((str(relative), "file"))
            continue
        if not stat.S_ISDIR(identity.st_mode):
            _fail("filesystem claim tree contains a non-regular entry")
        child_descriptor = os.open(child.name, DIRECTORY_FLAGS, dir_fd=descriptor)
        try:
            opened = os.fstat(child_descriptor)
            if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino):
                _fail("filesystem claim directory changed during observation")
            if (
                stat.S_IMODE(opened.st_mode) != MODE_DIRECTORY
                or opened.st_uid != os.geteuid()
                or opened.st_gid != os.getegid()
            ):
                _fail("filesystem claim directory is not private and executor-owned")
            entries.add((str(relative), "directory"))
            entries.update(_scan_tree(child_descriptor, relative))
        finally:
            os.close(child_descriptor)
    return entries


def _expected_entries(claim: JsonObject) -> set[TreeEntry]:
    desired = _object(claim.get("desired"), "filesystem desired")
    files = _objects(desired.get("owned_files"), "owned files")
    entries: set[TreeEntry] = set()
    for item in files:
        relative = PurePosixPath(_text(item.get("relative_path"), "owned path"))
        entries.add((str(relative), "file"))
        for parent in relative.parents:
            if str(parent) != ".":
                entries.add((str(parent), "directory"))
    return entries


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
