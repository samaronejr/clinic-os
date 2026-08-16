from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ops.testing import isolation_listener_metadata
from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Callable


def _process_stat(start_ticks: int = 98765, activity: int = 0) -> bytes:
    return (
        f"123 (python server) S 1 123 123 0 0 0 0 0 0 0 {activity} 0 0 0 "
        f"0 0 {1 + activity} 0 {start_ticks} 0 {10 + activity}\n"
    ).encode()


def _write_process_listener(tmp_path: Path) -> tuple[Path, Path]:
    proc_root = tmp_path / "proc"
    net_root = proc_root / "net"
    process_root = proc_root / "123"
    net_root.mkdir(parents=True)
    (process_root / "fd").mkdir(parents=True)
    tcp_row = (
        "0: 0100007F:1F90 00000000:0000 0A "
        f"00000000:00000000 00:00000000 00000000 {os.geteuid()} 0 4242\n"
    )
    (net_root / "tcp").write_text("header\n" + tcp_row)
    (net_root / "tcp6").write_text("header\n")
    (process_root / "stat").write_bytes(_process_stat())
    (process_root / "cmdline").write_bytes(b"python\x00server.py\x00")
    executable = tmp_path / "python"
    executable.write_bytes(b"synthetic executable")
    (process_root / "exe").symlink_to(executable)
    (process_root / "fd" / "7").symlink_to("socket:[4242]")
    return proc_root, process_root


def _after_read(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    action: Callable[[], object],
    *,
    occurrence: int = 1,
) -> None:
    target_inode = path.stat().st_ino
    real_read = os.read
    completed_reads = 0

    def read_then_act(descriptor: int, maximum: int) -> bytes:
        nonlocal completed_reads
        chunk = real_read(descriptor, maximum)
        if not chunk and os.fstat(descriptor).st_ino == target_inode:
            completed_reads += 1
            if completed_reads == occurrence:
                action()
        return chunk

    monkeypatch.setattr(os, "read", read_then_act)


def _add_socket_owner(proc_root: Path, pid: int) -> None:
    (proc_root / str(pid) / "fd").mkdir(parents=True)
    (proc_root / str(pid) / "fd" / "8").symlink_to("socket:[4242]")


def _capture_fails(proc_root: Path, message: str) -> None:
    with pytest.raises(IsolationError, match=message):
        isolation_listener_metadata.capture_loopback_listeners(proc_root, [])


def test_loopback_inventory_binds_socket_inode_to_exact_process_identity(
    tmp_path: Path,
) -> None:
    # Given: one executor-owned loopback socket and its synthetic proc identity.
    proc_root, process_root = _write_process_listener(tmp_path)
    command = b"python\x00server.py\x00"

    # When: the loopback reader joins socket and process metadata without signaling it.
    listeners = isolation_listener_metadata.capture_loopback_listeners(proc_root, [])

    # Then: the exact closed process-owned listener identity is emitted.
    assert listeners == [
        {
            "argv_sha256": hashlib.sha256(command).hexdigest(),
            "container_id": None,
            "executable_realpath": str((process_root / "exe").resolve(strict=True)),
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


def test_process_listener_accepts_volatile_stat_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: scheduler and memory fields change after the first synthetic stat read.
    proc_root, process_root = _write_process_listener(tmp_path)
    stat_path = process_root / "stat"
    _after_read(
        monkeypatch,
        stat_path,
        lambda: stat_path.write_bytes(_process_stat(activity=50)),
    )

    # When: the listener is captured across that harmless process activity.
    isolation_listener_metadata.capture_loopback_listeners(proc_root, [])


def test_process_listener_accepts_volatile_exe_link_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: only proc's executable-link timestamps change during capture.
    proc_root, process_root = _write_process_listener(tmp_path)
    executable_link = process_root / "exe"
    timestamps_before = executable_link.lstat().st_mtime_ns

    def change_timestamps() -> None:
        os.utime(
            executable_link,
            ns=(timestamps_before + 2_000_000_000, timestamps_before + 2_000_000_000),
            follow_symlinks=False,
        )

    _after_read(monkeypatch, process_root / "cmdline", change_timestamps)

    # When: the unchanged process generation is captured.
    isolation_listener_metadata.capture_loopback_listeners(proc_root, [])

    # Then: volatile symlink timestamps do not invalidate stable identity.
    assert executable_link.lstat().st_mtime_ns != timestamps_before


def test_process_listener_rejects_cmdline_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: argv changes after its first read for one otherwise stable process.
    proc_root, process_root = _write_process_listener(tmp_path)
    command_path = process_root / "cmdline"
    _after_read(
        monkeypatch,
        command_path,
        lambda: command_path.write_bytes(b"python\x00other.py\x00"),
    )

    # When/Then: capture fails closed instead of hashing a raced argv snapshot.
    _capture_fails(proc_root, "process identity changed during listener capture")


def test_process_listener_rejects_socket_gone_from_listening_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the socket disappears from the listening table during capture.
    proc_root, process_root = _write_process_listener(tmp_path)
    tcp_path = proc_root / "net" / "tcp"
    _after_read(
        monkeypatch,
        process_root / "cmdline",
        lambda: tcp_path.write_text("header\n"),
        occurrence=2,
    )

    # When/Then: a stale listener row cannot be emitted as current ownership.
    _capture_fails(proc_root, "process released listener during identity capture")


@pytest.mark.parametrize(
    ("changed_stat", "message"),
    [
        (_process_stat(98766), "process identity changed"),
        (b"123 (python server) S\n", "process stat has no start ticks"),
    ],
    ids=["changed-generation", "malformed-start-ticks"],
)
def test_process_listener_rejects_changed_or_malformed_start_ticks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_stat: bytes,
    message: str,
) -> None:
    # Given: the same PID path reports invalid stable generation metadata.
    proc_root, process_root = _write_process_listener(tmp_path)
    stat_path = process_root / "stat"
    _after_read(monkeypatch, stat_path, lambda: stat_path.write_bytes(changed_stat))

    # When/Then: capture rejects PID reuse and malformed identity alike.
    _capture_fails(proc_root, message)


def test_process_listener_rejects_executable_realpath_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: proc's stable exe entry resolves to a different target on reread.
    proc_root, process_root = _write_process_listener(tmp_path)
    executable_link = process_root / "exe"
    original = executable_link.resolve(strict=True)
    replacement = tmp_path / "python-replacement"
    replacement.write_bytes(b"replacement executable")
    real_resolve = Path.resolve
    resolutions = iter((original, replacement))

    def changing_resolve(path: Path, strict: bool = False) -> Path:
        if path == executable_link:
            return next(resolutions)
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", changing_resolve)

    # When/Then: same-PID re-exec cannot retain the prior listener identity.
    _capture_fails(proc_root, "process identity changed")


def test_process_listener_rejects_executable_link_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: proc's exe symlink inode changes while its target stays constant.
    proc_root, process_root = _write_process_listener(tmp_path)
    executable_link = process_root / "exe"
    target = executable_link.resolve(strict=True)

    def replace_link() -> None:
        executable_link.rename(process_root / "exe-before")
        executable_link.symlink_to(target)

    _after_read(monkeypatch, process_root / "cmdline", replace_link)

    # When/Then: changed stable symlink identity fails closed.
    _capture_fails(proc_root, "process identity changed")


@pytest.mark.parametrize("replacement_pid", [None, 456], ids=["released", "reassigned"])
def test_process_listener_rejects_released_or_reassigned_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_pid: int | None,
) -> None:
    # Given: the original process releases its FD during identity capture.
    proc_root, process_root = _write_process_listener(tmp_path)

    def change_owner() -> None:
        (process_root / "fd" / "7").unlink()
        if replacement_pid is not None:
            _add_socket_owner(proc_root, replacement_pid)

    _after_read(
        monkeypatch,
        process_root / "cmdline",
        change_owner,
        occurrence=2,
    )

    # When/Then: release and reassignment both invalidate the captured row.
    _capture_fails(proc_root, "process released listener")


def test_process_listener_rejects_multiple_owners(tmp_path: Path) -> None:
    # Given: two process FD tables claim the same listening socket inode.
    proc_root, _ = _write_process_listener(tmp_path)
    _add_socket_owner(proc_root, 456)

    # When/Then: ownership is ambiguous and capture fails closed.
    _capture_fails(proc_root, "ambiguous process ownership")


def test_process_listener_rejects_unreadable_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the only candidate process FD table cannot be inspected.
    proc_root, process_root = _write_process_listener(tmp_path)
    fd_root = process_root / "fd"
    real_scandir = os.scandir

    def deny_fd(path: str | os.PathLike[str]) -> object:
        if Path(path) == fd_root:
            raise PermissionError
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_fd)

    # When/Then: unreadable ownership remains ambiguous rather than assumed.
    _capture_fails(proc_root, "ambiguous process ownership")


def test_repeated_process_capture_survives_continuous_stat_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: every stat read advances volatile scheduler and memory counters.
    proc_root, process_root = _write_process_listener(tmp_path)
    stat_path = process_root / "stat"
    stat_inode = stat_path.stat().st_ino
    real_read = os.read
    mutations = 0

    def read_and_churn(descriptor: int, maximum: int) -> bytes:
        nonlocal mutations
        chunk = real_read(descriptor, maximum)
        if not chunk and os.fstat(descriptor).st_ino == stat_inode:
            mutations += 1
            stat_path.write_bytes(_process_stat(activity=mutations))
        return chunk

    monkeypatch.setattr(os, "read", read_and_churn)

    # When: the busy-but-unchanged process is captured repeatedly.
    captures = [
        isolation_listener_metadata.capture_loopback_listeners(proc_root, [])
        for _ in range(100)
    ]

    # Then: all captures are stable despite 200 distinct stat observations.
    assert mutations == 200
    assert captures == [captures[0]] * 100
