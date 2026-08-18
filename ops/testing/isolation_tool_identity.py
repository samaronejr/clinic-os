"""Authenticate launcher entries and resolved executable bytes."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Never

from ops.testing.isolation_common import IsolationError, JsonObject, stat_identity
from ops.testing.isolation_static_elf import require_static_amd64_elf


def authenticate_launcher(path: Path) -> JsonObject:
    """Return the closed source-entry and resolved-target identity."""
    if not path.is_absolute():
        _fail("launcher path is not absolute")
    source = path.lstat()
    source_kind = "symlink" if stat.S_ISLNK(source.st_mode) else "regular"
    if source_kind == "regular" and not stat.S_ISREG(source.st_mode):
        _fail("launcher source is neither a regular file nor a symlink")
    resolved = path.resolve(strict=True)
    target = resolved.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(target.st_mode)
        or target.st_uid != os.geteuid()
        or target.st_gid != os.getegid()
        or stat.S_IMODE(target.st_mode) & 0o111 == 0
    ):
        _fail("launcher target is not an executor-owned executable")
    digest = hashlib.sha256()
    descriptor = os.open(resolved, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return {
        "resolved_identity": stat_identity(resolved),
        "resolved_path": str(resolved),
        "resolved_sha256": digest.hexdigest(),
        "source_identity": stat_identity(path, follow_symlinks=False),
        "source_kind": source_kind,
        "source_path": str(path),
        "symlink_target": str(path.readlink()) if source_kind == "symlink" else None,
    }


def authenticate_static_amd64_launcher(path: Path) -> JsonObject:
    """Authenticate one launcher and require a static Linux/amd64 ELF target."""
    identity = authenticate_launcher(path)
    require_static_amd64_elf(Path(str(identity["resolved_path"])))
    return identity


def _fail(message: str) -> Never:
    raise IsolationError(message)
