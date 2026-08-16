"""Authenticate the sole Phase 1A authority namespace before ledger creation."""

from __future__ import annotations

import ctypes
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    IsolationError,
    JsonObject,
    directory_identity,
)

if TYPE_CHECKING:
    from pathlib import Path


RENAME_NOREPLACE: Final = 1
OMO_NAME: Final = ".omo"
EVIDENCE_NAME: Final = "evidence"
PENDING_NAME: Final = ".omo.phase1a-pending"


@dataclass(frozen=True, slots=True)
class NamespaceBinding:
    """Validated authority identities persisted into the first ledger."""

    authority_workspace: Path
    authority_root: Path
    authority_root_identity: JsonObject
    worktree_omo_lstat: JsonObject
    evidence_lstat: JsonObject


def bind_namespace(
    authority_workspace: Path,
    authority_root: Path,
    worktree: Path,
) -> NamespaceBinding:
    """Create or adopt only the exact authority symlink and evidence directory."""
    workspace = _canonical_directory(authority_workspace, "authority workspace")
    root = _canonical_directory(authority_root, "authority root")
    feature = _canonical_directory(worktree, "feature worktree")
    if root != workspace / OMO_NAME:
        message = "authority root is not the invoked workspace .omo"
        raise IsolationError(message)
    root_identity = directory_identity(root)
    root_identity.pop("link_count")
    link_text = os.path.relpath(root, start=feature)
    feature_descriptor = os.open(
        feature,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        created_link = _create_or_adopt_link(
            feature_descriptor,
            feature,
            root,
            link_text,
        )
    finally:
        os.close(feature_descriptor)
    worktree_lstat = _link_identity(feature / OMO_NAME, root, link_text, created_link)
    created_evidence = _create_or_adopt_evidence(root)
    evidence_lstat = _evidence_identity(root / EVIDENCE_NAME, created_evidence)
    return NamespaceBinding(
        authority_workspace=workspace,
        authority_root=root,
        authority_root_identity=root_identity,
        worktree_omo_lstat=worktree_lstat,
        evidence_lstat=evidence_lstat,
    )


def _canonical_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        message = f"{label} must be an absolute non-symlink directory"
        raise IsolationError(message)
    resolved = path.resolve(strict=True)
    if resolved != path:
        message = f"{label} is noncanonical"
        raise IsolationError(message)
    directory_identity(resolved)
    return resolved


def _create_or_adopt_link(
    directory: int,
    worktree: Path,
    root: Path,
    link_text: str,
) -> bool:
    try:
        existing = os.stat(OMO_NAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        _validate_link(directory, worktree, root, link_text, existing)
        return False
    try:
        os.symlink(link_text, PENDING_NAME, dir_fd=directory)
    except FileExistsError as error:
        message = "namespace pending name already exists"
        raise IsolationError(message) from error
    try:
        _rename_no_replace(directory, PENDING_NAME, OMO_NAME)
        os.fsync(directory)
    except OSError as error:
        try:
            os.unlink(PENDING_NAME, dir_fd=directory)
            os.fsync(directory)
        except FileNotFoundError:
            pass
        message = f"authority symlink publication failed: {error}"
        raise IsolationError(message) from error
    published = os.stat(OMO_NAME, dir_fd=directory, follow_symlinks=False)
    _validate_link(directory, worktree, root, link_text, published)
    return True


def _rename_no_replace(directory: int, source: str, destination: str) -> None:
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
        directory,
        source.encode(),
        directory,
        destination.encode(),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _validate_link(
    directory: int,
    worktree: Path,
    root: Path,
    link_text: str,
    value: os.stat_result,
) -> None:
    if (
        not stat.S_ISLNK(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or os.readlink(OMO_NAME, dir_fd=directory) != link_text
        or (worktree / OMO_NAME).resolve(strict=True) != root
    ):
        message = "feature worktree .omo is not the exact authority symlink"
        raise IsolationError(message)
    directory_identity(root)


def _create_or_adopt_evidence(root: Path) -> bool:
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.mkdir(EVIDENCE_NAME, mode=MODE_DIRECTORY, dir_fd=root_descriptor)
        except FileExistsError:
            directory_identity(root / EVIDENCE_NAME)
            return False
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)
    directory_identity(root / EVIDENCE_NAME)
    return True


def _link_identity(path: Path, root: Path, link_text: str, created: bool) -> JsonObject:
    value = path.lstat()
    return {
        "created_by_attempt": created,
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "link_target": link_text,
        "mode": stat.S_IMODE(value.st_mode),
        "path": str(path),
        "realpath": str(root),
        "type": "symlink-to-directory",
        "uid": value.st_uid,
    }


def _evidence_identity(path: Path, created: bool) -> JsonObject:
    value = path.stat(follow_symlinks=False)
    return {
        "created_by_attempt": created,
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "mode": stat.S_IMODE(value.st_mode),
        "path": str(path),
        "realpath": str(path.resolve(strict=True)),
        "type": "directory",
        "uid": value.st_uid,
    }
