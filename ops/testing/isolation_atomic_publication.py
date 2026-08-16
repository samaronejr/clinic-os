"""Publish immutable authorization bytes through a recoverable hard-link step."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import (
    MAX_JSON_BYTES,
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    fsync_directory,
    regular_identity,
)

TOKEN_PATTERN: Final = re.compile(r"^[0-9a-f-]{36}$")

if TYPE_CHECKING:
    from pathlib import Path


def _fail(message: str) -> Never:
    raise IsolationError(message)


def publish_immutable(destination: Path, raw: bytes, token: str) -> None:
    """Create or adopt one exact mode-0400 file without replacing its name."""
    if not destination.is_absolute() or TOKEN_PATTERN.fullmatch(token) is None:
        _fail("immutable publication path or token is invalid")
    pending = destination.with_name(f".{destination.name}.{token}.pending")
    destination_exists = _exists_nofollow(destination)
    pending_exists = _exists_nofollow(pending)
    if destination_exists:
        _finish_linked_prefix(destination, pending, raw, pending_exists)
        return
    if pending_exists:
        _require_bytes(pending, raw, links=1)
    else:
        _create_pending(pending, raw)
    try:
        os.link(pending, destination, follow_symlinks=False)
    except FileExistsError:
        _finish_linked_prefix(destination, pending, raw, pending_exists=True)
        return
    fsync_directory(destination.parent)
    pending.unlink()
    fsync_directory(destination.parent)
    _require_bytes(destination, raw, links=1)


def require_published_bytes(destination: Path, raw: bytes) -> None:
    """Authenticate one completed immutable publication by identity and bytes."""
    if not destination.is_absolute():
        _fail("published destination is not absolute")
    _require_bytes(destination, raw, links=1)


def _create_pending(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, MODE_PRIVATE)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
        os.fchmod(descriptor, MODE_IMMUTABLE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finish_linked_prefix(
    destination: Path,
    pending: Path,
    raw: bytes,
    pending_exists: bool,
) -> None:
    if pending_exists:
        destination_identity = regular_identity(
            destination, mode=MODE_IMMUTABLE, links=2
        )
        pending_identity = regular_identity(pending, mode=MODE_IMMUTABLE, links=2)
        if (
            destination_identity["device"],
            destination_identity["inode"],
        ) != (pending_identity["device"], pending_identity["inode"]):
            _fail("immutable publication pending inode is foreign")
        _require_bytes(destination, raw, links=2)
        pending.unlink()
        fsync_directory(destination.parent)
    _require_bytes(destination, raw, links=1)


def _require_bytes(path: Path, expected: bytes, *, links: int) -> None:
    regular_identity(path, mode=MODE_IMMUTABLE, links=links)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = b""
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    if raw != expected:
        _fail("immutable publication bytes do not match authorization")


def _exists_nofollow(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True
