"""Join observable loopback sockets to exact Docker or process identities."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

MAX_PROC_BYTES: Final = 4 * 1024 * 1024
LISTEN_STATE: Final = "0A"
MIN_SOCKET_FIELDS: Final = 10
MAX_PORT: Final = 65535


@dataclass(frozen=True, slots=True)
class _SocketRow:
    host: str
    port: int
    inode: int
    uid: int


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    start_ticks: int
    executable_realpath: Path
    executable_link: tuple[int, int, int, int, int]
    command: bytes


def _fail(message: str) -> Never:
    raise IsolationError(message)


def capture_loopback_listeners(
    proc_root: Path,
    containers: list[JsonValue],
) -> list[JsonValue]:
    """Capture executor-observable loopback listeners without signaling owners."""
    rows = _socket_rows(proc_root)
    container_owners = _container_owners(containers)
    process_targets = {
        row.inode
        for row in rows
        if (row.host, row.port) not in container_owners and row.uid == os.geteuid()
    }
    process_owners = _process_owners(proc_root, process_targets)
    listeners: list[JsonValue] = []
    for row in rows:
        container_id = container_owners.get((row.host, row.port))
        if container_id is not None:
            listeners.append(_container_listener(row, container_id))
            continue
        if row.uid != os.geteuid():
            continue
        pids = process_owners.get(row.inode, [])
        if len(pids) != 1:
            _fail("executor loopback listener has ambiguous process ownership")
        listeners.append(_process_listener(proc_root, row, pids[0]))
    return listeners


def _socket_rows(proc_root: Path) -> list[_SocketRow]:
    rows = [
        *_parse_socket_table(proc_root / "net" / "tcp", socket.AF_INET),
        *_parse_socket_table(proc_root / "net" / "tcp6", socket.AF_INET6),
    ]
    result = sorted(rows, key=lambda row: (row.host, row.port, row.inode))
    identities = {(row.host, row.port, row.inode) for row in result}
    if len(result) != len(identities):
        _fail("duplicate loopback socket row")
    return result


def _parse_socket_table(path: Path, family: socket.AddressFamily) -> list[_SocketRow]:
    text = _read_bytes(path, MAX_PROC_BYTES).decode()
    rows: list[_SocketRow] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < MIN_SOCKET_FIELDS or fields[3] != LISTEN_STATE:
            continue
        address, separator, port_text = fields[1].partition(":")
        if separator != ":":
            _fail("invalid proc socket address")
        host = _decode_host(address, family)
        if not ipaddress.ip_address(host).is_loopback:
            continue
        try:
            port = int(port_text, 16)
            uid = int(fields[7])
            inode = int(fields[9])
        except ValueError as error:
            message = "invalid numeric proc socket field"
            raise IsolationError(message) from error
        if not 1 <= port <= MAX_PORT or inode <= 0 or uid < 0:
            _fail("proc socket field is out of range")
        rows.append(_SocketRow(host=host, port=port, inode=inode, uid=uid))
    return rows


def _decode_host(value: str, family: socket.AddressFamily) -> str:
    try:
        raw = bytes.fromhex(value)
        if family == socket.AF_INET:
            packed = raw[::-1]
        else:
            packed = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
        return socket.inet_ntop(family, packed)
    except (OSError, ValueError) as error:
        message = "invalid proc socket host encoding"
        raise IsolationError(message) from error


def _container_owners(containers: list[JsonValue]) -> dict[tuple[str, int], str]:
    owners: dict[tuple[str, int], str] = {}
    for raw in containers:
        container = _object(raw, "container")
        identifier = _text(container["id"], "container id")
        ports = container["published_ports"]
        if not isinstance(ports, list):
            _fail("container published ports must be an array")
        for raw_port in ports:
            port = _object(raw_port, "published port")
            key = (
                _text(port["host"], "published host"),
                _integer(port["port"], "published port"),
            )
            previous = owners.setdefault(key, identifier)
            if previous != identifier:
                _fail("multiple containers own one published listener")
    return owners


def _process_owners(proc_root: Path, inodes: set[int]) -> dict[int, list[int]]:
    owners: dict[int, list[int]] = {}
    with os.scandir(proc_root) as processes:
        for process in processes:
            if not process.name.isdecimal() or not process.is_dir(
                follow_symlinks=False
            ):
                continue
            pid = int(process.name)
            try:
                descriptors = os.scandir(Path(process.path) / "fd")
            except (FileNotFoundError, PermissionError):
                continue
            with descriptors:
                for descriptor in descriptors:
                    try:
                        target = str(Path(descriptor.path).readlink())
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    inode = _socket_inode(target)
                    if inode in inodes:
                        owners.setdefault(inode, []).append(pid)
    return {inode: sorted(set(pids)) for inode, pids in owners.items()}


def _socket_inode(target: str) -> int:
    if not target.startswith("socket:[") or not target.endswith("]"):
        return -1
    try:
        return int(target[8:-1])
    except ValueError:
        return -1


def _process_listener(proc_root: Path, row: _SocketRow, pid: int) -> JsonObject:
    process_root = proc_root / str(pid)
    identity = _read_process_identity(process_root, pid)
    if _read_process_identity(process_root, pid) != identity:
        _fail("process identity changed during listener capture")
    if row not in _socket_rows(proc_root) or _process_owners(
        proc_root, {row.inode}
    ).get(row.inode) != [pid]:
        _fail("process released listener during identity capture")
    return {
        "argv_sha256": hashlib.sha256(identity.command).hexdigest(),
        "container_id": None,
        "executable_realpath": str(identity.executable_realpath),
        "host": row.host,
        "owner_kind": "process",
        "pid": pid,
        "port": row.port,
        "process_start_ticks": identity.start_ticks,
        "socket_inode": row.inode,
        "transport": "tcp",
    }


def _read_process_identity(process_root: Path, pid: int) -> _ProcessIdentity:
    start_ticks = _start_ticks(
        _read_bytes(process_root / "stat", MAX_PROC_BYTES),
        pid,
    )
    executable_link = process_root / "exe"
    metadata = executable_link.lstat()
    if not stat.S_ISLNK(metadata.st_mode):
        _fail("process executable entry is not a symlink")
    return _ProcessIdentity(
        start_ticks,
        executable_link.resolve(strict=True),
        (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
        ),
        _read_bytes(process_root / "cmdline", MAX_PROC_BYTES),
    )


def _container_listener(row: _SocketRow, container_id: str) -> JsonObject:
    return {
        "argv_sha256": None,
        "container_id": container_id,
        "executable_realpath": None,
        "host": row.host,
        "owner_kind": "container",
        "pid": None,
        "port": row.port,
        "process_start_ticks": None,
        "socket_inode": row.inode,
        "transport": "tcp",
    }


def _start_ticks(raw: bytes, pid: int) -> int:
    marker = raw.rfind(b")")
    prefix = f"{pid} (".encode()
    if not raw.startswith(prefix) or marker < len(prefix):
        _fail("invalid process stat identity")
    fields = raw[marker + 1 :].split()
    try:
        value = int(fields[19])
    except (IndexError, ValueError) as error:
        message = "process stat has no start ticks"
        raise IsolationError(message) from error
    if value < 0:
        _fail("process start ticks is negative")
    return value


def _read_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                return bytes(raw)
            raw.extend(chunk)
        _fail("proc metadata exceeds the size limit")
    finally:
        os.close(descriptor)


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{context} must be an object")
    return cast("JsonObject", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{context} must be an integer")
    return value
