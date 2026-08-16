"""Hash and no-replace move relocatable archive trees."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from typing import TYPE_CHECKING, Final, Never

import rfc8785

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    IsolationError,
    JsonObject,
    JsonValue,
    directory_identity,
    ensure_private_directory,
    fsync_directory,
    raw_sha256,
)

if TYPE_CHECKING:
    from pathlib import Path

AT_FDCWD: Final = -100
RENAME_NOREPLACE: Final = 1


def tree_sha256(root: Path) -> str:
    """Hash one tree from sorted relocatable no-follow identity records."""
    if root.is_symlink() or root.resolve(strict=True) != root:
        _fail("archive tree root is noncanonical")
    entries: list[JsonValue] = []
    _walk(root, root, entries)
    return raw_sha256(rfc8785.dumps(entries))


def ensure_bundle_directories(bundle: Path) -> None:
    """Create or validate the three private rejection-bundle directories."""
    for path in (bundle.parent.parent, bundle.parent, bundle):
        ensure_private_directory(path)
        identity = directory_identity(path)
        if identity.get("mode") != MODE_DIRECTORY:
            _fail("archive bundle directory is not private")


def move_no_replace(source: Path, destination: Path) -> None:
    """Rename one bound source without replacing any destination entry."""
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = library.renameat2
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)
    fsync_directory(source.parent)
    if destination.parent != source.parent:
        fsync_directory(destination.parent)


def _walk(root: Path, path: Path, entries: list[JsonValue]) -> None:
    value = path.lstat()
    relative = "." if path == root else path.relative_to(root).as_posix()
    record: JsonObject = {
        "gid": value.st_gid,
        "link_count": value.st_nlink,
        "mode": stat.S_IMODE(value.st_mode),
        "relative_path": relative,
        "uid": value.st_uid,
    }
    if stat.S_ISDIR(value.st_mode):
        record["type"] = "directory"
        entries.append(record)
        for child in sorted(path.iterdir(), key=lambda item: os.fsencode(item.name)):
            _walk(root, child, entries)
        return
    if stat.S_ISREG(value.st_mode):
        digest, size = _hash_regular(path, value)
        record.update({"sha256": digest, "size": size, "type": "regular"})
        entries.append(record)
        return
    if stat.S_ISLNK(value.st_mode):
        target = str(path.readlink())
        if path.lstat() != value:
            _fail("archive symlink identity changed while hashing")
        record.update({"link_target": target, "type": "symlink"})
        entries.append(record)
        return
    _fail(f"unsupported archive tree entry: {relative}")


def _hash_regular(path: Path, expected: os.stat_result) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    size = 0
    try:
        if os.fstat(descriptor) != expected:
            _fail("archive file identity changed before hashing")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        if os.fstat(descriptor) != expected or path.lstat() != expected:
            _fail("archive file identity changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _fail(message: str) -> Never:
    raise IsolationError(message)
