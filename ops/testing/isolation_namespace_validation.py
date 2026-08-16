"""Revalidate the authenticated authority namespace during locked transitions."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    directory_identity,
)
from ops.testing.isolation_namespace import EVIDENCE_NAME, OMO_NAME, NamespaceBinding

if TYPE_CHECKING:
    from pathlib import Path


def validate_namespace_binding(binding: NamespaceBinding, worktree: Path) -> None:
    """Require every captured namespace name and inode to remain unchanged."""
    try:
        _validate_binding(binding, worktree)
    except (IsolationError, OSError, RuntimeError) as error:
        message = "authority namespace binding drifted"
        raise IsolationError(message) from error


def _validate_binding(binding: NamespaceBinding, worktree: Path) -> None:
    workspace = _canonical_directory(binding.authority_workspace)
    root = _canonical_directory(binding.authority_root)
    feature = _canonical_directory(worktree)
    if root != workspace / OMO_NAME:
        _fail()
    root_identity = directory_identity(root)
    root_identity.pop("link_count")
    if root_identity != binding.authority_root_identity:
        _fail()
    link_path = feature / OMO_NAME
    if _link_record(link_path) != _without_creation(binding.worktree_omo_lstat):
        _fail()
    evidence_path = root / EVIDENCE_NAME
    if _evidence_record(evidence_path) != _without_creation(binding.evidence_lstat):
        _fail()


def _canonical_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        _fail()
    resolved = path.resolve(strict=True)
    if resolved != path:
        _fail()
    directory_identity(resolved)
    return resolved


def _link_record(path: Path) -> JsonObject:
    value = path.lstat()
    if not stat.S_ISLNK(value.st_mode):
        _fail()
    target = str(path.readlink())
    return {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "link_target": target,
        "mode": stat.S_IMODE(value.st_mode),
        "path": str(path),
        "realpath": str(path.resolve(strict=True)),
        "type": "symlink-to-directory",
        "uid": value.st_uid,
    }


def _evidence_record(path: Path) -> JsonObject:
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(value.st_mode):
        _fail()
    return {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "mode": stat.S_IMODE(value.st_mode),
        "path": str(path),
        "realpath": str(path.resolve(strict=True)),
        "type": "directory",
        "uid": value.st_uid,
    }


def _without_creation(value: JsonObject) -> JsonObject:
    if not isinstance(value.get("created_by_attempt"), bool):
        _fail()
    return {key: item for key, item in value.items() if key != "created_by_attempt"}


def _fail() -> Never:
    message = "authority namespace binding drifted"
    raise IsolationError(message)
