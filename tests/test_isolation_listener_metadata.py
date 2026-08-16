from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path

import pytest
from ops.testing import isolation_listener_metadata


def test_loopback_inventory_binds_socket_inode_to_exact_process_identity(
    tmp_path: Path,
) -> None:
    # Given: one executor-owned loopback socket and its synthetic proc identity.
    proc_root = tmp_path / "proc"
    net_root = proc_root / "net"
    process_root = proc_root / "123"
    fd_root = process_root / "fd"
    net_root.mkdir(parents=True)
    fd_root.mkdir(parents=True)
    tcp_row = (
        "0: 0100007F:1F90 00000000:0000 0A "
        f"00000000:00000000 00:00000000 00000000 {os.geteuid()} 0 4242\n"
    )
    (net_root / "tcp").write_text("header\n" + tcp_row)
    (net_root / "tcp6").write_text("header\n")
    (process_root / "stat").write_text(
        "123 (python server) S 1 123 123 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 98765\n"
    )
    command = b"python\x00server.py\x00"
    (process_root / "cmdline").write_bytes(command)
    (process_root / "exe").symlink_to(Path("/proc/self/exe").resolve(strict=True))
    (fd_root / "7").symlink_to("socket:[4242]")

    # When: the loopback reader joins socket and process metadata without signaling it.
    try:
        module = importlib.import_module("ops.testing.isolation_listener_metadata")
    except ModuleNotFoundError:
        pytest.fail("listener metadata reader is missing")
    listeners = module.capture_loopback_listeners(proc_root, [])

    # Then: the exact closed process-owned listener identity is emitted.
    assert listeners == [
        {
            "argv_sha256": hashlib.sha256(command).hexdigest(),
            "container_id": None,
            "executable_realpath": str(Path("/proc/self/exe").resolve(strict=True)),
            "host": "127.0.0.1",
            "owner_kind": "process",
            "pid": 123,
            "port": 8080,
            "process_start_ticks": 98765,
            "socket_inode": 4242,
            "transport": "tcp",
        }
    ]


def test_bounded_proc_reader_accepts_multiple_short_reads_before_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a bounded proc-like stream that returns two short chunks before EOF.
    path = tmp_path / "proc-metadata"
    path.write_bytes(b"unused")
    chunks = iter((b"a", b"b", b""))

    def read_chunk(_descriptor: int, _maximum: int) -> bytes:
        return next(chunks)

    monkeypatch.setattr(os, "read", read_chunk)

    # When: the bounded reader consumes the stream.
    actual = isolation_listener_metadata._read_bytes(path, 2)

    # Then: short chunks are accumulated until EOF without a false overflow.
    assert actual == b"ab"
