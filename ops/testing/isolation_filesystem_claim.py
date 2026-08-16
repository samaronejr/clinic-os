"""Verify filesystem claim observations without following staging symlinks."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Final, Never, cast

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
    regular_identity,
)

OBSERVED_KEYS: Final = frozenset({"owned_files", "published_outputs"})
OBSERVED_FILE_KEYS: Final = frozenset(
    {"relative_path", "mode", "uid", "gid", "device", "inode", "sha256"}
)


def _fail(message: str) -> Never:
    raise IsolationError(message)


def load_filesystem_observation(
    path: Path,
    claim: JsonObject,
    claim_root: Path,
) -> JsonObject:
    """Re-observe every staged file and require exact immutable input equality."""
    if not path.is_absolute() or path.is_symlink():
        _fail("observed filesystem input must be an absolute non-symlink file")
    regular_identity(path, mode=MODE_IMMUTABLE)
    observed, _ = load_json(path)
    if set(observed) != OBSERVED_KEYS:
        _fail("filesystem observation has the wrong closed key set")
    expected = load_current_filesystem_observation(claim, claim_root)
    if observed != expected:
        _fail("filesystem observation does not match fresh staging identity")
    return observed


def load_current_filesystem_observation(
    claim: JsonObject,
    claim_root: Path,
) -> JsonObject:
    """Build the exact current ordinary filesystem observation."""
    desired = _object(claim["desired"], "filesystem desired")
    return {
        "owned_files": [
            _observe_file(claim_root, item)
            for item in _objects(desired["owned_files"], "desired owned files")
        ],
        "published_outputs": _unpublished_outputs(desired["published_outputs"]),
    }


def require_filesystem_release_ready(claim: JsonObject, claim_root: Path) -> None:
    """Require mutable staging absent and authorization state internally empty."""
    try:
        os.lstat(claim_root)
    except FileNotFoundError:
        pass
    else:
        _fail("claim root remains present")
    observed = _object(claim["observed"], "filesystem observed")
    outputs = _objects(observed["published_outputs"], "published outputs")
    for output in outputs:
        status = output.get("status")
        entries = output.get("entries")
        if status == "unpublished" and entries != []:
            _fail("unpublished output contains entries")
        if status not in {"unpublished", "published"}:
            _fail("published output status is invalid")


def _observe_file(root: Path, desired: JsonObject) -> JsonObject:
    relative = _text(desired["relative_path"], "owned-file relative path")
    parts = PurePosixPath(relative).parts
    descriptor = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    opened: list[int] = [descriptor]
    try:
        for part in parts[:-1]:
            descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            opened.append(descriptor)
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
        opened.append(file_descriptor)
        identity = os.fstat(file_descriptor)
        if not stat.S_ISREG(identity.st_mode):
            _fail("owned staging path is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(file_descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        for opened_descriptor in reversed(opened):
            os.close(opened_descriptor)
    record: JsonObject = {
        "device": identity.st_dev,
        "gid": identity.st_gid,
        "inode": identity.st_ino,
        "mode": stat.S_IMODE(identity.st_mode),
        "relative_path": relative,
        "sha256": digest.hexdigest(),
        "uid": identity.st_uid,
    }
    if any(record[key] != desired[key] for key in ("mode", "uid", "gid", "sha256")):
        _fail("owned staging file drifted from its desired identity")
    return record


def _unpublished_outputs(value: JsonValue) -> list[JsonValue]:
    outputs = _objects(value, "published-output authorizations")
    return [
        {
            "authorization_id": authorization["authorization_id"],
            "entries": [],
            "governing_lock": authorization["governing_lock"],
            "output_kind": authorization["output_kind"],
            "root_path": authorization["root_path"],
            "status": "unpublished",
        }
        for authorization in outputs
    ]


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
