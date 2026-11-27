"""The Landlock layer confines a child by kernel policy, and refuses to be absent."""

import ctypes
import errno
import socket
import subprocess
import textwrap
from pathlib import Path

import pytest
from ops.testing import migration_upgrade_landlock as landlock

MODULE = Path(landlock.__file__)


class _Kernel:
    """A libc whose landlock_create_ruleset answers as a given kernel would."""

    def __init__(self, answer: int, code: int = 0) -> None:
        self.answer = answer
        self.code = code

    def syscall(self, *_arguments: object) -> int:
        if self.answer < 0:
            ctypes.set_errno(self.code)
        return self.answer


def test_a_kernel_without_landlock_is_refused_by_name() -> None:
    with pytest.raises(landlock.LandlockUnavailableError, match="landlock_unavailable"):
        landlock.abi(_Kernel(-1, errno.ENOSYS))  # type: ignore[arg-type]
    with pytest.raises(landlock.LandlockUnavailableError, match="landlock_unavailable"):
        landlock.apply([], [], libc=_Kernel(-1, errno.EOPNOTSUPP))  # type: ignore[arg-type]


def test_a_kernel_too_old_to_cover_udp_is_refused_by_name() -> None:
    with pytest.raises(landlock.LandlockUnavailableError, match="landlock_abi_too_old"):
        landlock.abi(_Kernel(landlock.MIN_ABI - 1))  # type: ignore[arg-type]


def test_this_host_enforces_a_usable_abi() -> None:
    assert landlock.abi() >= landlock.MIN_ABI


CONFINED = textwrap.dedent(
    """
    import errno, os, socket, sys, tempfile
    sys.path.insert(0, os.path.dirname(sys.argv[1]))
    import migration_upgrade_landlock as landlock

    base = tempfile.mkdtemp(prefix="landlock.")
    run, foreign = os.path.join(base, "run"), os.path.join(base, "foreign")
    os.mkdir(run)
    os.mkdir(foreign)
    victim = os.path.join(foreign, "victim")
    open(victim, "w").write("ORIGINAL")
    os.symlink(victim, os.path.join(run, "planted"))
    sock_path = os.path.join(foreign, "s.sock")
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock_path)
    server.listen(1)
    held = os.open(run, os.O_PATH)
    version = landlock.apply(
        [held], ["/usr", "/lib", "/lib64", "/etc", sys.prefix, sys.base_prefix, foreign]
    )

    def attempt(label, action):
        try:
            action()
            print(label, "ALLOWED")
        except OSError as error:
            print(label, errno.errorcode.get(error.errno, error.errno))

    def reopen():
        return open(f"/proc/self/fd/{os.open(victim, os.O_RDONLY)}", "w").write("PWN")

    attempt("inside-write", lambda: open(os.path.join(run, "ok"), "w").write("x"))
    attempt("inside-mkdir", lambda: os.mkdir(os.path.join(run, "d")))
    ok, ok2 = os.path.join(run, "ok"), os.path.join(run, "ok2")
    attempt("inside-rename", lambda: os.rename(ok, ok2))
    planted = os.path.join(run, "planted")
    attempt("planted-link", lambda: open(planted, "w").write("PWN"))
    attempt("foreign-write", lambda: open(victim, "w").write("PWN"))
    attempt("foreign-append", lambda: open(victim, "a").write("PWN"))
    attempt("foreign-truncate", lambda: os.truncate(victim, 0))
    attempt("foreign-unlink", lambda: os.unlink(victim))
    attempt("foreign-rename", lambda: os.rename(victim, victim + "2"))
    attempt("foreign-create", lambda: open(os.path.join(foreign, "new"), "w"))
    attempt("foreign-mkdir", lambda: os.mkdir(os.path.join(foreign, "nd")))
    hard = os.path.join(run, "hard")
    attempt("foreign-hardlink-in", lambda: os.link(victim, hard))
    attempt("proc-fd-reopen", reopen)
    attempt("foreign-read", lambda: open(victim).read())
    if version >= 9:
        attempt("unix-socket", lambda: socket.socket(socket.AF_UNIX).connect(sock_path))
    print("VICTIM", open(victim).read())
    """
)


def test_the_kernel_refuses_every_write_outside_the_one_root() -> None:
    result = subprocess.run(  # noqa: S603 - fixed probe in a throwaway process
        ["/usr/bin/python3", "-c", CONFINED, str(MODULE)],
        capture_output=True,
        text=True,
        check=False,
    )

    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines())
    assert result.returncode == 0, result.stderr
    for allowed in ("inside-write", "inside-mkdir", "inside-rename", "foreign-read"):
        assert lines[allowed] == "ALLOWED", (allowed, result.stdout)
    for refused in (
        "planted-link",
        "foreign-write",
        "foreign-append",
        "foreign-truncate",
        "foreign-unlink",
        "foreign-rename",
        "foreign-create",
        "foreign-mkdir",
        "foreign-hardlink-in",
        "proc-fd-reopen",
    ):
        assert lines[refused] != "ALLOWED", (refused, result.stdout)
    assert lines["VICTIM"] == "ORIGINAL"
    if "unix-socket" in lines:
        assert lines["unix-socket"] == "EACCES"


@pytest.mark.skipif(landlock.abi() < 4, reason="TCP rules need Landlock ABI 4")
def test_tcp_reaches_only_the_named_port() -> None:
    probe = textwrap.dedent(
        """
        import errno, os, socket, sys
        sys.path.insert(0, os.path.dirname(sys.argv[1]))
        import migration_upgrade_landlock as landlock
        allowed, other = int(sys.argv[2]), int(sys.argv[3])
        landlock.apply([], ["/usr", "/lib", "/lib64", "/etc", sys.prefix], [allowed])
        for label, port in (("allowed", allowed), ("other", other)):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=5).close()
                print(label, "CONNECTED")
            except OSError as error:
                print(label, errno.errorcode.get(error.errno, error.errno))
        """
    )
    first, second = socket.socket(), socket.socket()
    try:
        for server in (first, second):
            server.bind(("127.0.0.1", 0))
            server.listen(4)
        ports = [first.getsockname()[1], second.getsockname()[1]]
        result = subprocess.run(  # noqa: S603 - fixed probe in a throwaway process
            ["/usr/bin/python3", "-c", probe, str(MODULE), *map(str, ports)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        first.close()
        second.close()

    assert result.stdout.splitlines() == ["allowed CONNECTED", "other EACCES"], (
        result.stdout + result.stderr
    )


UDP_PROBE = textwrap.dedent(
    """
    import errno, os, socket, sys
    sys.path.insert(0, os.path.dirname(sys.argv[1]))
    import migration_upgrade_landlock as landlock

    mode, datagram_port, tcp_port = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    landlock.apply(
        [],
        ["/usr", "/lib", "/lib64", "/etc", sys.prefix],
        [tcp_port] if mode == "ports" else None,
    )

    def attempt(label, action):
        try:
            action()
            print(label, "ALLOWED")
        except OSError as error:
            print(label, errno.errorcode.get(error.errno, error.errno))

    def datagram(port):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(b"x", ("127.0.0.1", port))

    def bind():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(("127.0.0.1", 0))

    def connect():
        socket.create_connection(("127.0.0.1", tcp_port), timeout=5).close()

    attempt("send-host", lambda: datagram(datagram_port))
    attempt("send-dns", lambda: datagram(53))
    attempt("bind", bind)
    attempt("resolve", lambda: socket.getaddrinfo("localhost", tcp_port))
    attempt("tcp", connect)
    """
)


def _probe_udp(mode: str) -> dict[str, str]:
    """Run the confined probe; the host listener must have received nothing."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener,
        socket.socket() as server,
    ):
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(0)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        result = subprocess.run(  # noqa: S603 - fixed probe in a throwaway process
            [
                "/usr/bin/python3",
                "-c",
                UDP_PROBE,
                str(MODULE),
                mode,
                str(listener.getsockname()[1]),
                str(server.getsockname()[1]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        with pytest.raises(BlockingIOError):
            listener.recv(1)
    return dict(line.split(" ", 1) for line in result.stdout.splitlines())


def test_no_datagram_is_sent_bound_or_connected_while_tcp_ports_are_named() -> None:
    assert _probe_udp("ports") == {
        "send-host": "EACCES",
        "send-dns": "EACCES",
        "bind": "EACCES",
        "resolve": "ALLOWED",
        "tcp": "ALLOWED",
    }


def test_a_fetching_child_reaches_only_the_dns_port_over_udp() -> None:
    seen = _probe_udp("open")

    assert seen["send-host"] == "EACCES"
    assert seen["send-dns"] == "ALLOWED"
    assert seen["tcp"] == "ALLOWED"
