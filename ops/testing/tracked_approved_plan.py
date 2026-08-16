"""Authenticate the committed approved-plan pair used by tracked CI."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Final, Never

from ops.testing.approved_plan import ApprovedPlan
from ops.testing.isolation_common import IsolationError

PLAN_RELATIVE_PATH: Final = Path("docs/plans/clinic-os-phase1a-approved.md")
SIDECAR_RELATIVE_PATH: Final = PLAN_RELATIVE_PATH.with_suffix(".sha256")
SIDECAR_PATTERN: Final = re.compile(rb"[0-9a-f]{64}\n")
READ_CHUNK_SIZE: Final = 1024 * 1024
GIT_INDEX_FIELD_COUNT: Final = 3


def authenticate_tracked_plan(
    source: Path,
    sidecar: Path,
    worktree: Path,
) -> ApprovedPlan:
    """Bind exact working-tree bytes to the committed hosted-CI plan pair."""
    root = _canonical_worktree(worktree)
    expected_source = root / PLAN_RELATIVE_PATH
    expected_sidecar = root / SIDECAR_RELATIVE_PATH
    if source != expected_source or sidecar != expected_sidecar:
        _fail("tracked approved-plan paths are not canonical")
    raw = _read_regular(source, "tracked approved plan")
    sidecar_raw = _read_regular(sidecar, "tracked approved-plan sidecar")
    if SIDECAR_PATTERN.fullmatch(sidecar_raw) is None:
        _fail("tracked approved-plan sidecar is not lowercase SHA-256 plus LF")
    digest = hashlib.sha256(raw).hexdigest()
    if sidecar_raw != f"{digest}\n".encode():
        _fail("tracked approved-plan bytes and sidecar disagree")
    _require_committed(root, PLAN_RELATIVE_PATH, raw)
    _require_committed(root, SIDECAR_RELATIVE_PATH, sidecar_raw)
    return ApprovedPlan(
        source_kind="tracked-ci",
        path=source,
        sha256=digest,
        sidecar_path=sidecar,
    )


def _canonical_worktree(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        _fail("tracked-CI worktree must be an absolute non-symlink directory")
    resolved = path.resolve(strict=True)
    value = resolved.stat(follow_symlinks=False)
    if (
        resolved != path
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
    ):
        _fail("tracked-CI worktree is not canonical and executor-owned")
    top_level = _git(root=resolved, arguments=("rev-parse", "--show-toplevel"))
    try:
        git_root = Path(top_level.decode().strip()).resolve(strict=True)
    except UnicodeDecodeError as error:
        message = "Git returned a non-textual worktree root"
        raise IsolationError(message) from error
    if git_root != resolved:
        _fail("tracked-CI worktree is not the Git checkout root")
    return resolved


def _read_regular(path: Path, label: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        _require_tracked_identity(before, label)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, READ_CHUNK_SIZE):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = path.lstat()
    finally:
        os.close(descriptor)
    if _identity(after) != _identity(before) or _identity(named) != _identity(before):
        _fail(f"{label} changed while being authenticated")
    return b"".join(chunks)


def _require_tracked_identity(value: os.stat_result, label: str) -> None:
    mode = stat.S_IMODE(value.st_mode)
    forbidden = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o111
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or value.st_nlink != 1
        or mode & forbidden
    ):
        _fail(f"{label} has invalid tracked-file identity")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
    )


def _require_committed(root: Path, relative: Path, current: bytes) -> None:
    relative_text = relative.as_posix()
    staged = _git(
        root=root,
        arguments=("ls-files", "--stage", "--", relative_text),
    )
    lines = staged.splitlines()
    if len(lines) != 1 or b"\t" not in lines[0]:
        _fail(f"tracked approved-plan input is not committed: {relative_text}")
    metadata, staged_path = lines[0].split(b"\t", 1)
    fields = metadata.split()
    if (
        fields[:1] != [b"100644"]
        or len(fields) != GIT_INDEX_FIELD_COUNT
        or fields[2] != b"0"
        or staged_path.decode(errors="surrogateescape") != relative_text
    ):
        _fail(f"tracked approved-plan index identity drifted: {relative_text}")
    committed = _git(root=root, arguments=("show", f"HEAD:{relative_text}"))
    if committed != current:
        _fail(f"tracked approved-plan bytes are not committed: {relative_text}")


def _git(*, root: Path, arguments: tuple[str, ...]) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        _fail("Git is required to authenticate tracked approved-plan inputs")
    result = subprocess.run(  # noqa: S603 - fixed executable and closed arguments.
        (executable, "-C", str(root), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        _fail("tracked approved-plan input is not committed")
    return result.stdout


def _fail(message: str) -> Never:
    raise IsolationError(message)
