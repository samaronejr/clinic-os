"""Create and authenticate private logical-recovery transport artifacts."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import TYPE_CHECKING, Never

from ops.testing.restore_contract import RestoreContractError

if TYPE_CHECKING:
    from pathlib import Path

PRIVATE_MODE = 0o600


def write_private_archive(path: Path, raw: bytes) -> None:
    """Create one mode-0600 archive without replacing an existing path."""
    if not path.is_absolute() or not raw:
        _fail("recovery archive path or content is invalid")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_MODE)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent(path)


def write_hash_sidecar(archive: Path, sidecar: Path) -> None:
    """Create the exact mode-0600 SHA-256 sidecar for one archive."""
    _require_private_regular(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    raw = f"{digest}  {archive}\n".encode("ascii")
    descriptor = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_MODE)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent(sidecar)


def verify_hash_sidecar(archive: Path, sidecar: Path) -> None:
    """Reject mode, identity, grammar, or digest drift before restore."""
    _require_private_regular(archive)
    _require_private_regular(sidecar)
    raw = sidecar.read_bytes()
    expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected = f"{expected_digest}  {archive}\n".encode("ascii")
    if not hmac.compare_digest(raw, expected):
        _fail("recovery archive SHA-256 verification failed")


def delete_private_artifacts(*paths: Path) -> None:
    """Remove transient dump/hash/credential paths and prove absence."""
    parents: set[Path] = set()
    for path in paths:
        if path.exists() or path.is_symlink():
            _require_private_regular(path)
            path.unlink()
            parents.add(path.parent)
    for parent in parents:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if any(path.exists() or path.is_symlink() for path in paths):
        _fail("recovery transient artifact remained")


def _require_private_regular(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o777 != PRIVATE_MODE
        or path.stat().st_nlink != 1
    ):
        _fail("recovery artifact identity is invalid")


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fail(reason: str) -> Never:
    raise RestoreContractError(reason)
