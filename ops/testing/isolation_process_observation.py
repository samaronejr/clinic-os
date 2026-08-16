"""Authenticate live process identities against reserved claim observations."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Final, Never, cast

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
    regular_identity,
)
from ops.testing.isolation_process_topology import validate_process_topology

MAX_PROC_BYTES: Final = 4 * 1024 * 1024
START_TICKS_INDEX: Final = 19
STATUS_IDENTITY_FIELD_COUNT: Final = 2


def _fail(message: str) -> Never:
    raise IsolationError(message)


def load_process_observation(
    path: Path,
    claim: JsonObject,
    claim_root: Path,
) -> JsonObject:
    """Load one immutable observation and reauthenticate every procfs member."""
    if not path.is_absolute() or path.is_symlink():
        _fail("process observation must be an absolute non-symlink file")
    regular_identity(path, mode=MODE_IMMUTABLE)
    _require_private_root(claim_root)
    observed, _ = load_json(path)
    validate_process_observation(observed, claim)
    supplied = _objects(observed["members"], "process members")
    actual = [
        observe_process_member(_integer(item.get("pid"), "member PID"))
        for item in supplied
    ]
    if supplied != actual:
        _fail("process member identity drifted during activation")
    _require_observed_socket_owners(observed, actual)
    return observed


def validate_process_observation(observed: JsonObject, claim: JsonObject) -> None:
    """Authenticate closed process topology and every desired executable field."""
    desired = _object(claim.get("desired"), "process desired")
    validate_process_topology(observed, desired)
    for member in _objects(observed.get("members"), "process members"):
        if (
            member.get("uid") != desired.get("uid")
            or member.get("gid") != desired.get("gid")
            or member.get("executable_realpath") != desired.get("interpreter_realpath")
            or member.get("argv_sha256") != desired.get("argv_sha256")
        ):
            _fail("process member identity disagrees with desired executable")


def require_process_release_ready(claim: JsonObject, claim_root: Path) -> None:
    """Require an absent root and absence of every recorded PID/start identity."""
    try:
        os.lstat(claim_root)
    except FileNotFoundError:
        pass
    else:
        _fail("claim root still exists")
    observed = _object(claim.get("observed"), "process observation")
    for member in _objects(observed.get("members"), "process members"):
        pid = _integer(member.get("pid"), "member PID")
        start_ticks = _integer(member.get("start_ticks"), "member start ticks")
        if _same_process_identity(pid, start_ticks):
            _fail("live process identity remains recorded")
    if observed.get("listeners") not in ([], None) and any(
        _same_process_identity(
            _integer(item.get("pid"), "listener PID"),
            _integer(item.get("process_start_ticks"), "listener start ticks"),
        )
        for item in _objects(observed.get("listeners"), "process listeners")
    ):
        _fail("live process listener remains recorded")


def observe_process_member(pid: int, proc_root: Path = Path("/proc")) -> JsonObject:
    """Read one PID twice and return its stable executable/start identity."""
    root = proc_root / str(pid)
    try:
        stat_before = _read_proc(root / "stat")
        link_before = (root / "exe").lstat()
        executable = (root / "exe").resolve(strict=True)
        command = _read_proc(root / "cmdline")
        status = _read_proc(root / "status")
        stat_after = _read_proc(root / "stat")
        link_after = (root / "exe").lstat()
    except FileNotFoundError as error:
        message = "process identity disappeared during activation"
        raise IsolationError(message) from error
    if stat_before != stat_after or link_before != link_after:
        _fail("process identity changed during activation")
    ppid, pgid, sid, start_ticks = _stat_identity(stat_before, pid)
    uid, gid = _status_identity(status)
    return {
        "argv_sha256": hashlib.sha256(command).hexdigest(),
        "executable_realpath": str(executable),
        "gid": gid,
        "pgid": pgid,
        "pid": pid,
        "ppid": ppid,
        "sid": sid,
        "start_ticks": start_ticks,
        "uid": uid,
    }


def _require_observed_socket_owners(
    observed: JsonObject,
    members: list[JsonObject],
) -> None:
    socket_inode = observed["listener_socket_inode"]
    if socket_inode is None:
        return
    inode = _integer(socket_inode, "listener socket inode")
    for member in members:
        fd_root = Path("/proc") / str(member["pid"]) / "fd"
        if not _has_socket_inode(fd_root, inode):
            _fail("process member lost the shared listener socket")


def _has_socket_inode(fd_root: Path, inode: int) -> bool:
    try:
        descriptors = os.scandir(fd_root)
    except FileNotFoundError:
        return False
    with descriptors:
        return any(
            _readlink_or_none(Path(item.path)) == f"socket:[{inode}]"
            for item in descriptors
        )


def _readlink_or_none(path: Path) -> str | None:
    try:
        return str(path.readlink())
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _same_process_identity(pid: int, start_ticks: int) -> bool:
    try:
        raw = _read_proc(Path("/proc") / str(pid) / "stat")
    except FileNotFoundError:
        return False
    return _stat_identity(raw, pid)[3] == start_ticks


def _require_private_root(path: Path) -> None:
    value = os.lstat(path)
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_IMODE(value.st_mode) != MODE_DIRECTORY
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
    ):
        _fail("process claim root is not a private executor directory")


def _read_proc(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = b""
        while len(raw) <= MAX_PROC_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_PROC_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > MAX_PROC_BYTES:
            _fail("process metadata exceeds the bounded reader")
        return raw
    finally:
        os.close(descriptor)


def _stat_identity(raw: bytes, pid: int) -> tuple[int, int, int, int]:
    marker = raw.rfind(b")")
    if not raw.startswith(f"{pid} (".encode()) or marker < 0:
        _fail("process stat identity is malformed")
    fields = raw[marker + 1 :].split()
    try:
        return int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])
    except (IndexError, ValueError) as error:
        message = "process stat identity is incomplete"
        raise IsolationError(message) from error


def _status_identity(raw: bytes) -> tuple[int, int]:
    values: dict[bytes, int] = {}
    for line in raw.splitlines():
        key, separator, tail = line.partition(b":")
        if separator and key in {b"Uid", b"Gid"}:
            fields = tail.split()
            if len(fields) < STATUS_IDENTITY_FIELD_COUNT:
                _fail("process status identity is incomplete")
            values[key] = int(fields[1])
    if set(values) != {b"Uid", b"Gid"}:
        _fail("process status lacks UID/GID identity")
    return values[b"Uid"], values[b"Gid"]


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{context} is invalid")
    return value


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)
