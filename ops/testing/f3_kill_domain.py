"""Authenticate and operate the one F3 cgroup-v2 descendant kill domain."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ATTEMPT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
WAIT_SECONDS: Final = 30.0


class KillDomainError(RuntimeError):
    """Reject an unauthenticated, replaced, foreign, or populated cgroup."""


def _fail(reason: str) -> Never:
    raise KillDomainError(reason)


@dataclass(frozen=True, slots=True)
class PathIdentity:
    """Bind one nofollow directory path to its durable device and inode."""

    path: Path
    device: int
    inode: int


def path_identity(path: Path) -> PathIdentity:
    """Capture one absolute non-symlink directory identity without resolving it."""
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        _fail("kill-domain path identity rejected")
    status = path.stat(follow_symlinks=False)
    return PathIdentity(path, status.st_dev, status.st_ino)


def domain_path(parent: PathIdentity, attempt_id: str) -> Path:
    """Derive the sole child cgroup path from its frozen parent and attempt."""
    if ATTEMPT.fullmatch(attempt_id) is None:
        _fail("kill-domain attempt ID rejected")
    require_identity(parent.path, parent)
    return parent.path / f"clinic-os-phase1a-{attempt_id}-f3"


def require_identity(path: Path, expected: PathIdentity) -> None:
    """Reject path substitution, symlinks, or device/inode replacement."""
    observed = path_identity(path)
    if observed != expected:
        _fail("kill-domain identity drifted")


def require_empty_domain(path: Path, expected: PathIdentity) -> None:
    """Require exact identity, no descendants, no PIDs, and populated zero."""
    require_identity(path, expected)
    allowed = {"cgroup.events", "cgroup.procs", "cgroup.kill"}
    children = [item for item in path.iterdir() if item.is_dir()]
    if children:
        _fail("kill-domain contains a descendant cgroup")
    for name in allowed:
        candidate = path / name
        if candidate.is_symlink() or not candidate.is_file():
            _fail("kill-domain control file rejected")
    if (path / "cgroup.procs").read_text(encoding="ascii").strip():
        _fail("kill-domain contains a process")
    if _populated(path) != 0:
        _fail("kill-domain is populated")


def create_domain(
    parent: PathIdentity,
    attempt_id: str,
    record_intent: Callable[[Path], None],
) -> PathIdentity:
    """Fsync intent through the caller before creating the exact child cgroup."""
    path = domain_path(parent, attempt_id)
    if path.exists() or path.is_symlink():
        _fail("kill-domain path already exists")
    record_intent(path)
    path.mkdir(mode=0o700)
    identity = path_identity(path)
    require_empty_domain(path, identity)
    return identity


def move_process(identity: PathIdentity, pid: int) -> None:
    """Move one barrier-blocked child into the domain and verify membership."""
    if pid < 1:
        _fail("kill-domain PID rejected")
    require_identity(identity.path, identity)
    (identity.path / "cgroup.procs").write_text(f"{pid}\n", encoding="ascii")
    members = {
        int(item)
        for item in (identity.path / "cgroup.procs").read_text(encoding="ascii").split()
        if item.isdecimal()
    }
    if pid not in members:
        _fail("kill-domain membership was not established")


def kill_and_wait_empty(identity: PathIdentity, seconds: float = WAIT_SECONDS) -> None:
    """Use cgroup.kill and require the complete descendant tree to disappear."""
    require_identity(identity.path, identity)
    (identity.path / "cgroup.kill").write_text("1\n", encoding="ascii")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _populated(identity.path) == 0:
            return
        time.sleep(0.05)
    _fail("kill-domain remained populated")


def remove_empty_domain(identity: PathIdentity) -> None:
    """Remove only the same authenticated empty child cgroup inode."""
    require_empty_domain(identity.path, identity)
    identity.path.rmdir()
    if identity.path.exists() or identity.path.is_symlink():
        _fail("kill-domain removal failed")


def _populated(path: Path) -> int:
    values: dict[str, str] = {}
    for line in (path / "cgroup.events").read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition(" ")
        if not separator or key in values:
            _fail("kill-domain events are malformed")
        values[key] = value
    if values.get("populated") not in {"0", "1"}:
        _fail("kill-domain populated state is malformed")
    return int(values["populated"])
