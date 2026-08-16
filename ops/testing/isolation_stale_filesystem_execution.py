"""Remove only identity-bound files from one stale private claim root."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Final, Never, cast

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    IsolationError,
    JsonObject,
    JsonValue,
)

DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def remove_stale_staging(identity: JsonObject) -> None:
    """Nofollow-validate and durably unlink the exact authorized staging tree."""
    root = _absolute_path(identity.get("claim_root_path"))
    expected = _expected_files(identity)
    allowed_directories = {
        str(parent)
        for relative in expected
        for parent in PurePosixPath(relative).parents
        if str(parent) != "."
    }
    try:
        parent_descriptor = os.open(root.parent, DIRECTORY_FLAGS)
    except FileNotFoundError:
        return
    try:
        try:
            root_descriptor = os.open(
                root.name,
                DIRECTORY_FLAGS,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return
        try:
            root_identity = _require_private_directory(root_descriptor)
            _clean_directory(
                root_descriptor,
                PurePosixPath(),
                expected,
                allowed_directories,
            )
            named = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if _inode(named) != _inode(root_identity):
                _fail("stale claim root changed during cleanup")
        finally:
            os.close(root_descriptor)
        os.rmdir(root.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _clean_directory(
    descriptor: int,
    parent: PurePosixPath,
    expected: dict[str, JsonObject],
    allowed_directories: set[str],
) -> None:
    with os.scandir(descriptor) as iterator:
        children = sorted(iterator, key=lambda item: item.name)
    for child in children:
        relative = parent / child.name
        path = str(relative)
        observed = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            if path not in allowed_directories:
                _fail("stale staging contains an unauthorized directory")
            child_descriptor = os.open(child.name, DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                opened = _require_private_directory(child_descriptor)
                if _inode(opened) != _inode(observed):
                    _fail("stale staging directory changed during cleanup")
                _clean_directory(
                    child_descriptor,
                    relative,
                    expected,
                    allowed_directories,
                )
            finally:
                os.close(child_descriptor)
            os.rmdir(child.name, dir_fd=descriptor)
            os.fsync(descriptor)
            continue
        if not stat.S_ISREG(observed.st_mode):
            _fail("stale staging contains a non-regular entry")
        record = expected.get(path)
        if record is None:
            _fail("stale staging contains an unauthorized file")
        _unlink_authenticated_file(descriptor, child.name, observed, record)


def _unlink_authenticated_file(
    parent_descriptor: int,
    name: str,
    observed: os.stat_result,
    expected: JsonObject,
) -> None:
    descriptor = os.open(name, FILE_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if _inode(opened) != _inode(observed):
            _fail("stale staging file changed during cleanup")
        _require_file_identity(opened, expected)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
            size += len(chunk)
        if digest.hexdigest() != expected.get("sha256"):
            _fail("stale staging file hash drifted")
        expected_size = expected.get("size_bytes")
        if expected_size is not None and size != expected_size:
            _fail("stale staging file size drifted")
    finally:
        os.close(descriptor)
    os.unlink(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _expected_files(identity: JsonObject) -> dict[str, JsonObject]:
    candidate = identity.get("expected_staged_entry")
    if candidate is not None:
        if not isinstance(candidate, dict):
            _fail("candidate staged entry is not an object")
        values = [candidate]
    else:
        desired = _objects(identity.get("desired_owned_files"), "desired files")
        observed = _objects(identity.get("observed_owned_files"), "observed files")
        desired_by_path = {_relative(item): item for item in desired}
        if len(desired_by_path) != len(desired):
            _fail("stale desired file paths are duplicated")
        if observed:
            values = observed
            for item in observed:
                desired_item = desired_by_path.get(_relative(item))
                if desired_item is None or any(
                    item.get(key) != desired_item.get(key)
                    for key in ("mode", "uid", "gid", "sha256")
                ):
                    _fail("stale desired and observed file identities disagree")
        else:
            values = desired
    result = {_relative(item): item for item in values}
    if len(result) != len(values):
        _fail("stale staging file paths are duplicated")
    return result


def _require_file_identity(value: os.stat_result, expected: JsonObject) -> None:
    actual = {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "mode": stat.S_IMODE(value.st_mode),
        "uid": value.st_uid,
    }
    for key in ("mode", "uid", "gid", "device", "inode"):
        required = expected.get(key)
        if required is not None and actual[key] != required:
            _fail(f"stale staging file {key} drifted")


def _require_private_directory(descriptor: int) -> os.stat_result:
    value = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_IMODE(value.st_mode) != MODE_DIRECTORY
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
    ):
        _fail("stale claim directory is not private and executor-owned")
    return value


def _relative(value: JsonObject) -> str:
    raw = value.get("relative_path")
    if not isinstance(raw, str):
        _fail("stale staging relative path is not a string")
    path = PurePosixPath(raw)
    if path.is_absolute() or str(path) != raw or ".." in path.parts or raw == ".":
        _fail("stale staging relative path escapes its claim root")
    return raw


def _absolute_path(value: JsonValue) -> Path:
    if not isinstance(value, str):
        _fail("stale claim root is not a string")
    path = Path(value)
    if not path.is_absolute() or path.parent == path:
        _fail("stale claim root is not an absolute child path")
    return path


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _inode(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _fail(message: str) -> Never:
    raise IsolationError(message)
