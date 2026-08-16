"""Crash-safe primitives shared by the Phase 1A isolation commands."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


MAX_JSON_BYTES: Final = 16 * 1024 * 1024
MODE_PRIVATE: Final = 0o600
MODE_IMMUTABLE: Final = 0o400
MODE_DIRECTORY: Final = 0o700

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


@dataclass(slots=True)
class IsolationError(Exception):
    """Typed failure raised when an isolation boundary is not provable."""

    message: str

    def __str__(self) -> str:
        """Return the stable operator-facing contract failure."""
        return self.message


def canonical_bytes(value: JsonValue) -> bytes:
    """Return the repository's deterministic JSON record encoding."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{text}\n".encode()


def canonical_sha256(value: JsonValue) -> str:
    """Hash canonical JSON including its required trailing newline."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def raw_sha256(raw: bytes) -> str:
    """Hash an immutable byte sequence."""
    return hashlib.sha256(raw).hexdigest()


def utc_now() -> str:
    """Return the closed six-fraction UTC timestamp form."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_json(path: Path) -> tuple[JsonObject, bytes]:
    """Read one bounded no-follow canonical JSON object."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = b""
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > MAX_JSON_BYTES:
            message = f"JSON exceeds {MAX_JSON_BYTES} bytes: {path}"
            raise IsolationError(message)
    finally:
        os.close(descriptor)
    try:
        value: JsonValue = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = f"invalid JSON at {path}: {error}"
        raise IsolationError(message) from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        message = f"expected a JSON object at {path}"
        raise IsolationError(message)
    normalized = {str(key): item for key, item in value.items()}
    if canonical_bytes(normalized) != raw:
        message = f"JSON is not canonical at {path}"
        raise IsolationError(message)
    return normalized, raw


def stat_identity(path: Path, *, follow_symlinks: bool = False) -> JsonObject:
    """Capture the closed inode identity used by the ledger."""
    value = path.stat(follow_symlinks=follow_symlinks)
    return {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "link_count": value.st_nlink,
        "mode": stat.S_IMODE(value.st_mode),
        "uid": value.st_uid,
    }


def directory_identity(path: Path) -> JsonObject:
    """Validate and capture an executor-owned directory."""
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(value.st_mode):
        message = f"not a directory: {path}"
        raise IsolationError(message)
    if value.st_uid != os.geteuid() or value.st_gid != os.getegid():
        message = f"directory is not executor-owned: {path}"
        raise IsolationError(message)
    return stat_identity(path)


def regular_identity(path: Path, *, mode: int, links: int = 1) -> JsonObject:
    """Validate and capture an executor-owned regular file."""
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode):
        message = f"not a regular file: {path}"
        raise IsolationError(message)
    if (
        stat.S_IMODE(value.st_mode) != mode
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or value.st_nlink != links
    ):
        message = f"invalid file identity: {path}"
        raise IsolationError(message)
    return stat_identity(path)


def fsync_directory(path: Path) -> None:
    """Persist one directory entry update."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_no_replace(path: Path, raw: bytes, *, mode: int) -> None:
    """Publish one file exactly once with file and parent durability."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, MODE_PRIVATE)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def write_atomic_replace(path: Path, raw: bytes) -> None:
    """Replace one private state file using a same-directory durable temp."""
    pending = path.with_name(f".{path.name}.{os.getpid()}.pending")
    write_no_replace(pending, raw, mode=MODE_PRIVATE)
    try:
        pending.replace(path)
        fsync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            pending.unlink()


def ensure_private_directory(path: Path) -> bool:
    """Create an absent private directory or validate the existing owner."""
    try:
        path.mkdir(mode=MODE_DIRECTORY)
    except FileExistsError:
        directory_identity(path)
        return False
    fsync_directory(path.parent)
    directory_identity(path)
    return True


@contextmanager
def stable_lock(path: Path, *, create: bool) -> Iterator[tuple[int, JsonObject]]:
    """Hold and revalidate the canonical stable ledger lock inode."""
    created = _create_durable_lock(path) if create else None
    descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = regular_identity(path, mode=MODE_PRIVATE)
        if created is not None and before != created:
            message = "stable lock identity changed before nofollow reopen"
            raise IsolationError(message)
        if stat_identity_from_fd(descriptor) != before:
            message = "stable lock identity changed before flock"
            raise IsolationError(message)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        revalidate_held_lock(path, descriptor, before)
        yield descriptor, before
        revalidate_held_lock(path, descriptor, before)
    finally:
        os.close(descriptor)


def _create_durable_lock(path: Path) -> JsonObject:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, MODE_PRIVATE)
    except FileExistsError as error:
        message = "stable lock already exists"
        raise IsolationError(message) from error
    try:
        created = regular_identity(path, mode=MODE_PRIVATE)
        if stat_identity_from_fd(descriptor) != created:
            message = "stable lock identity changed during creation"
            raise IsolationError(message)
        os.fsync(descriptor)
        fsync_directory(path.parent)
        revalidate_held_lock(path, descriptor, created)
        return created
    finally:
        os.close(descriptor)


def revalidate_held_lock(
    path: Path,
    descriptor: int,
    expected: JsonObject,
) -> None:
    """Require the named stable lock and held description to remain identical."""
    try:
        named = regular_identity(path, mode=MODE_PRIVATE)
        held = stat_identity_from_fd(descriptor)
    except (IsolationError, OSError) as error:
        message = "stable lock identity changed while held"
        raise IsolationError(message) from error
    if named != expected or held != expected:
        message = "stable lock identity changed while held"
        raise IsolationError(message)


def stat_identity_from_fd(descriptor: int) -> JsonObject:
    """Capture the inode identity for an already-open file description."""
    value = os.fstat(descriptor)
    return {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "link_count": value.st_nlink,
        "mode": stat.S_IMODE(value.st_mode),
        "uid": value.st_uid,
    }
