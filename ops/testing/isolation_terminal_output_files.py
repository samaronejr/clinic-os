"""Validate immutable terminal output observations through no-follow opens."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Final, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

OBSERVATION_KEYS: Final = {
    "authorization_id",
    "entries",
    "governing_lock",
    "output_kind",
    "root_path",
    "status",
}
ENTRY_KEYS: Final = {
    "gid",
    "mode",
    "relative_path",
    "sha256",
    "size_bytes",
    "uid",
}
DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


def validate_output_observation(
    authorization: JsonObject,
    observation: JsonObject,
    root: Path,
    published: set[str],
) -> None:
    """Require one authorization state and its destination bytes to agree."""
    if set(observation) != OBSERVATION_KEYS:
        _fail("terminal publisher observation shape is open")
    for key in ("authorization_id", "governing_lock", "output_kind", "root_path"):
        if observation.get(key) != authorization.get(key):
            _fail("terminal publisher observation differs from authorization")
    paths = _strings(authorization.get("relative_paths"), "relative paths")
    entries = _objects(observation.get("entries"), "published entries")
    predecessors = _strings(
        authorization.get("predecessor_authorization_ids"), "predecessors"
    )
    if observation.get("status") == "unpublished":
        if entries or any(not _path_absent(root, path) for path in paths):
            _fail("unpublished terminal authorization has a destination")
        return
    if observation.get("status") != "published" or not set(predecessors) <= published:
        _fail("terminal authorization is not a predecessor-valid prefix")
    if [item.get("relative_path") for item in entries] != paths:
        _fail("published terminal authorization entries are incomplete")
    for entry in entries:
        _validate_published_entry(root, entry)


def canonical_relative_path(value: JsonValue) -> PurePosixPath:
    """Return a bounded terminal path that cannot escape its open root."""
    if not isinstance(value, str):
        _fail("terminal relative path is not a string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        _fail("terminal relative path escaped its authorization root")
    return path


def _validate_published_entry(root: Path, entry: JsonObject) -> None:
    if set(entry) != ENTRY_KEYS:
        _fail("terminal publisher entry shape is open")
    relative = canonical_relative_path(entry.get("relative_path"))
    descriptor = _open_file(root, relative)
    try:
        identity = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(descriptor)
    actual = {
        "gid": identity.st_gid,
        "mode": stat.S_IMODE(identity.st_mode),
        "relative_path": str(relative),
        "sha256": digest.hexdigest(),
        "size_bytes": size,
        "uid": identity.st_uid,
    }
    if not stat.S_ISREG(identity.st_mode) or actual != entry:
        _fail("published terminal entry bytes or identity drifted")


def _path_absent(root: Path, value: str) -> bool:
    relative = canonical_relative_path(value)
    try:
        descriptor = _open_file(root, relative)
    except FileNotFoundError:
        return True
    except OSError as error:
        message = "terminal destination could not be classified without following links"
        raise IsolationError(message) from error
    os.close(descriptor)
    return False


def _open_file(root: Path, relative: PurePosixPath) -> int:
    descriptor = os.open(root, DIRECTORY_FLAGS)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        result = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)
    return result


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)


def _fail(message: str) -> Never:
    raise IsolationError(message)
