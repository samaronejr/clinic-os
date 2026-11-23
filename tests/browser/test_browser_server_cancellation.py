"""A stop or kill of the harness at any point releases the server's port.

Every case first records what it observes (server processes gone, port not
listening, a plain bind possible, caller mask kept) and asserts that record;
cleanup of anything a failing run leaves behind happens only in ``finally``.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import http.client
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pytest
from ops.testing import browser_server_controller as controller
from ops.testing import browser_server_supervisor as supervisor
from ops.testing import renewal_runner as runner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import FrameType

    Tap = Callable[[str | None], object]

REPOSITORY = Path(__file__).resolve().parents[2]
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
TCP_LISTEN = "0A"
HARNESS_SECONDS = 60
# Kernel-delivered deaths are immediate; this bound stays well inside the
# controller's own 60 s readiness deadline, which would end a leak itself.
DEATH_SECONDS = 30
# Answers with the server's process group; /hold then keeps its worker busy
# for good, so only a signal (not Gunicorn's idle parent check) ends it. The
# held connection lingers 0 s, so the worker's death resets it rather than
# leaving the server side in TIME_WAIT (readiness clients reset theirs).
STUB_APP_SOURCE = (
    "import os, socket, struct, threading\n"
    "def application(environ, start_response):\n"
    "    line = f'{os.getpgid(0)}\\n'.encode()\n"
    "    hold = environ['PATH_INFO'] == '/hold'\n"
    "    if hold:\n"
    "        environ['gunicorn.socket'].setsockopt(\n"
    "            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))\n"
    "    length = str(len(line) + hold)\n"
    "    start_response('200 OK', [('Content-Length', length)])\n"
    "    yield line\n"
    "    if hold:\n"
    "        threading.Event().wait()\n"
)
# Django settings that hold the real config.wsgi preload in the master: it
# reports its pid, its parent (the controller), its group and its inherited
# port, then waits until a signal ends it.
HOLD_SETTINGS_SOURCE = (
    "import json, os, signal, socket, sys\n"
    "fd = int(sys.argv[sys.argv.index('--bind') + 1].removeprefix('fd://'))\n"
    "with socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM) as listener:\n"
    "    port = listener.getsockname()[1]\n"
    "with open(os.environ['CPI_FLAKE3_HOLD_FIFO'], 'w') as fifo:\n"
    "    fifo.write(json.dumps([os.getpid(), os.getppid(), os.getpgid(0), port]))\n"
    "signal.pause()\n"
)


def _unexpected_readiness(*_args: object) -> None:
    pytest.fail("the stop did not unwind the launcher during the handoff")


def _caller_mask() -> set[int]:
    return set(signal.pthread_sigmask(signal.SIG_BLOCK, ()))


def _listening(port: int) -> bool:
    """Report whether any IPv4/IPv6 TCP socket listens on ``port``."""
    hexport = f":{port:04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in Path(table).read_text(encoding="ascii").splitlines()[1:]:
            local, state = row.split()[1], row.split()[3]
            if local.endswith(hexport) and state == TCP_LISTEN:
                return True
    return False


def _plain_bindable(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind((controller.HOST, port))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            return False
    return True


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _prepare_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _tap: Tap
) -> Callable[[], object]:
    for name in controller.REQUIRED_ENVIRONMENT:
        monkeypatch.setenv(name, "synthetic")
    monkeypatch.setenv("CLINIC_BROWSER_EVIDENCE_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["browser_server_controller", "suite", "patient"])
    monkeypatch.setattr(controller, "wait_until_ready", _unexpected_readiness)
    return controller.main


def _prepare_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tap: Tap
) -> Callable[[], object]:
    monkeypatch.setenv("CLINIC_RENEWAL_APP_DATABASE_URL", "synthetic")
    monkeypatch.setattr(runner, "_resolve_browser", lambda _: "/unused")
    monkeypatch.setattr(runner, "_validate_serving_dsn", lambda value: value)
    monkeypatch.setattr(runner, "_capture_source", lambda *_: ({}, "digest"))
    monkeypatch.setattr(runner, "_require_app_role", lambda _: None)
    monkeypatch.setattr(runner, "_synthetic_secret_store", lambda _: nullcontext({}))
    monkeypatch.setattr(
        runner,
        "realtime_broker",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        runner, "_server_environment", lambda _: {"REALTIME_REDIS_URL": "synthetic"}
    )
    monkeypatch.setattr(runner, "broker_tap", tap)
    monkeypatch.setattr(runner, "wait_until_ready", _unexpected_readiness)
    return partial(
        runner._run_browser_suite,
        tmp_path,
        "journey-reception-pressure",
        tmp_path,
        tmp_path,
        None,
    )


class _Case(NamedTuple):
    prepare: Callable[[Path, pytest.MonkeyPatch, Tap], Callable[[], object]]
    sig: signal.Signals
    # The handler in force when the launcher starts.
    handler: Callable[[int, FrameType | None], object]
    unwind: type[BaseException]


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _Case(
                _prepare_controller,
                signal.SIGINT,
                signal.default_int_handler,
                KeyboardInterrupt,
            ),
            id="controller-ctrl-c",
        ),
        pytest.param(
            _Case(
                _prepare_renewal,
                signal.SIGTERM,
                runner._interrupted,
                runner.RenewalInterruptError,
            ),
            id="renewal-stop",
        ),
    ],
)
def test_stop_before_the_handle_is_stored_releases_the_port(
    case: _Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sig = case.sig
    request_interrupt = threading.Event()
    signal_delivered = threading.Event()
    tap_threads: list[threading.Thread] = []
    launched: list[tuple[subprocess.Popen[bytes], int]] = []
    initialize = subprocess.Popen.__init__
    mask_before = _caller_mask()

    def interrupt_from_tap() -> None:
        if request_interrupt.wait(timeout=10):
            signal.raise_signal(sig)
            signal_delivered.set()

    @contextmanager
    def tap(_url: str | None) -> Iterator[list[bytes]]:
        thread = threading.Thread(target=interrupt_from_tap, daemon=True)
        thread.start()
        tap_threads.append(thread)
        try:
            yield []
        finally:
            thread.join(timeout=11)

    def spawn_then_interrupt(
        child: subprocess.Popen[bytes], argv: list[str], **kwargs: object
    ) -> None:
        fd = int(argv[argv.index("--bind") + 1].removeprefix("fd://"))
        with socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM) as listener:
            port = int(listener.getsockname()[1])
        # A real child inherits the listener and only a signal ends it.
        initialize(
            child,
            [sys.executable, "-c", "import signal; signal.pause()"],
            **cast("Any", kwargs),
        )
        launched.append((child, port))
        # The child runs and the launcher has no handle yet. The stop goes to a
        # running helper thread, as the kernel would pick a thread that does
        # not block it; without one it goes to this thread.
        if tap_threads:
            request_interrupt.set()
            assert signal_delivered.wait(timeout=5)
        else:
            signal.raise_signal(sig)

    monkeypatch.setattr(subprocess.Popen, "__init__", spawn_then_interrupt)
    run = case.prepare(tmp_path, monkeypatch, tap)
    previous = signal.signal(sig, case.handler)
    try:
        with pytest.raises(case.unwind):
            run()
        assert len(launched) == 1
        child, port = launched[0]
        observed = {
            "launcher_reaped": child.returncode,
            "alive": _alive(child.pid),
            "listening": _listening(port),
            "plain_bind": _plain_bindable(port),
            "mask_kept": _caller_mask() == mask_before,
        }
        assert observed == {
            "launcher_reaped": -signal.SIGTERM,
            "alive": False,
            "listening": False,
            "plain_bind": True,
            "mask_kept": True,
        }
    finally:
        signal.signal(sig, previous)
        for child, _port in launched:
            if child.returncode is None:
                child.kill()
                child.wait(timeout=10)


def test_reservation_is_exclusive_before_listen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_socket = socket.socket
    peers: list[socket.socket] = []
    refusals: list[int | None] = []
    reserve_errors: list[int | None] = []
    reserved: list[int] = []

    class PeerAtListen(socket.socket):
        def listen(self, *args: int) -> None:
            # A reuse-enabled peer tries the same endpoint between our bind
            # and our listen, the only point where the port is not yet owned.
            peer = real_socket(socket.AF_INET, socket.SOCK_STREAM)
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                peer.bind(self.getsockname())
                peer.listen()
                peers.append(peer)
            except OSError as error:
                refusals.append(error.errno)
                peer.close()
            super().listen(*args)

    monkeypatch.setattr(socket, "socket", PeerAtListen)
    try:
        try:
            with controller.reserve_port() as (port, _listener_fd):
                reserved.append(port)
        except OSError as error:
            reserve_errors.append(error.errno)
        observed = {
            "peer_listening": len(peers),
            "peer_refused": refusals,
            "reserve_errors": reserve_errors,
            "reserved": len(reserved),
        }
        assert observed == {
            "peer_listening": 0,
            "peer_refused": [errno.EADDRINUSE],
            "reserve_errors": [],
            "reserved": 1,
        }
    finally:
        for peer in peers:
            peer.close()


class _MaskInterruptError(Exception):
    """A handled signal raised at the return of the mask change."""


def test_interrupted_mask_block_restores_the_caller_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sigmask = signal.pthread_sigmask
    spawned: list[list[str]] = []
    mask_before = _caller_mask()

    def block_then_interrupt(
        how: int, mask: tuple[int, ...] | frozenset[int]
    ) -> set[int]:
        previous = real_sigmask(how, mask)
        if how == signal.SIG_BLOCK and mask:
            raise _MaskInterruptError
        return previous

    def record_spawn(argv: list[str], **_kwargs: object) -> None:
        spawned.append(argv)

    monkeypatch.setattr(signal, "pthread_sigmask", block_then_interrupt)
    monkeypatch.setattr(subprocess, "Popen", record_spawn)
    try:
        with pytest.raises(_MaskInterruptError):
            supervisor.start_master(
                [sys.executable, "-c", "pass"], {}, tmp_path / "mask.log", owner=[]
            )
        observed = {
            "mask_kept": set(real_sigmask(signal.SIG_BLOCK, ())) == mask_before,
            "spawned": spawned,
        }
        assert observed == {"mask_kept": True, "spawned": []}
    finally:
        real_sigmask(signal.SIG_SETMASK, mask_before)


class _AlarmError(Exception):
    """The caller's SIGALRM handler, raised at entry to the mask restore."""


def _alarm(_signum: int, _frame: object) -> None:
    raise _AlarmError


def test_signal_at_the_restore_on_an_error_path_keeps_the_caller_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sigmask = signal.pthread_sigmask
    mask_before = _caller_mask()

    def alarm_before_restore(
        how: int, mask: tuple[int, ...] | frozenset[int]
    ) -> set[int]:
        if how == signal.SIG_SETMASK:
            # The caller's alarm fires just before the restoring syscall.
            signal.raise_signal(signal.SIGALRM)
        return real_sigmask(how, mask)

    previous_alarm = signal.signal(signal.SIGALRM, _alarm)
    monkeypatch.setattr(signal, "pthread_sigmask", alarm_before_restore)
    try:
        # The log path is a directory: the launch fails before any spawn.
        with pytest.raises(_AlarmError):
            supervisor.start_master(
                [sys.executable, "-c", "pass"], {}, tmp_path, owner=[]
            )
        observed = {"mask_kept": set(real_sigmask(signal.SIG_BLOCK, ())) == mask_before}
        assert observed == {"mask_kept": True}
    finally:
        real_sigmask(signal.SIG_SETMASK, mask_before)
        signal.signal(signal.SIGALRM, previous_alarm)


def test_missing_pthread_sigmask_fails_closed_at_launch_for_both_launchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []

    def record_spawn(argv: list[str], **_kwargs: object) -> None:
        started.append("spawn")

    def record_fork() -> int:
        started.append("fork")
        raise AssertionError

    monkeypatch.delattr(signal, "pthread_sigmask")
    monkeypatch.setattr(subprocess, "Popen", record_spawn)
    monkeypatch.setattr(os, "fork", record_fork)
    reasons: list[str] = []
    for launch in (
        partial(
            supervisor.start_master,
            [sys.executable, "-c", "pass"],
            {},
            tmp_path / "api.log",
            owner=[],
        ),
        supervisor.main,
    ):
        with pytest.raises(supervisor.SupervisorError) as rejected:
            launch()
        reasons.append(str(rejected.value))
    observed = {"started": started, "reasons": reasons}
    reason = "browser supervisor rejected: signal.pthread_sigmask is unavailable"
    assert observed == {
        "started": [],
        "reasons": [f"{reason} on this Python build"] * 2,
    }


def test_missing_libc_prctl_fails_at_launch_not_import() -> None:
    probe = (
        "import ctypes\n"
        "class NoPrctl(ctypes.CDLL):\n"
        "    def __getattr__(self, name):\n"
        "        if name == 'prctl':\n"
        "            raise AttributeError(name)\n"
        "        return super().__getattr__(name)\n"
        "ctypes.CDLL = NoPrctl\n"
        "from ops.testing import browser_server_supervisor as supervisor\n"
        "print('imported')\n"
        "try:\n"
        "    supervisor.require_process_controls()\n"
        "except supervisor.SupervisorError as error:\n"
        "    print(error)\n"
    )
    completed = subprocess.run(  # noqa: S603 - this interpreter, fixed probe.
        [sys.executable, "-c", probe],
        cwd=REPOSITORY,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY)},
        capture_output=True,
        text=True,
        timeout=HARNESS_SECONDS,
        check=False,
    )
    assert (completed.returncode, completed.stdout.splitlines()) == (
        0,
        [
            "imported",
            "browser supervisor rejected: libc prctl is unavailable; "
            "the server cannot follow its harness",
        ],
    )


@contextmanager
def _child_subreaper() -> Iterator[None]:
    """Adopt orphaned descendants so the test can wait for the server."""
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, *[ctypes.c_ulong] * 4]
    prctl.restype = ctypes.c_int
    saved = ctypes.c_int()
    assert prctl(PR_GET_CHILD_SUBREAPER, ctypes.addressof(saved), 0, 0, 0) == 0
    assert prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        prctl(PR_SET_CHILD_SUBREAPER, saved.value, 0, 0, 0)


def _reap(targets: tuple[int, ...], done: threading.Event) -> None:
    """Reap adopted pids or ``-pgid`` groups; ECHILD means none is left."""
    for target in targets:
        with contextlib.suppress(ChildProcessError):
            while True:
                os.waitpid(target, 0)
    done.set()


class _Harness(NamedTuple):
    module: str
    mode: tuple[str, ...]
    stop: signal.Signals
    exit_signal: signal.Signals


def _harness_environment(tmp_path: Path, *, preload: bool) -> dict[str, str]:
    (tmp_path / "cpi_flake3_stop_app.py").write_text(STUB_APP_SOURCE, encoding="utf-8")
    (tmp_path / "cpi_flake3_hold_settings.py").write_text(
        HOLD_SETTINGS_SOURCE, encoding="utf-8"
    )
    hold = {"DJANGO_SETTINGS_MODULE": "cpi_flake3_hold_settings"} if preload else {}
    return {
        **os.environ,
        **dict.fromkeys(controller.REQUIRED_ENVIRONMENT, "synthetic"),
        "CLINIC_BROWSER_EVIDENCE_ROOT": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(
            (str(tmp_path), str(REPOSITORY), str(REPOSITORY / "tests"))
        ),
        # Gunicorn's control socket stays inside this test's directory.
        "XDG_RUNTIME_DIR": str(tmp_path),
        "CPI_FLAKE3_HOLD_FIFO": str(tmp_path / "hold.fifo"),
        **hold,
    }


def _held_server(
    harness: subprocess.Popen[str], held: list[http.client.HTTPResponse]
) -> tuple[int, int, int]:
    """Hold one worker in a request; return (port, pgid, no separate controller)."""
    assert harness.stdout is not None
    assert select.select([harness.stdout], [], [], HARNESS_SECONDS)[0]
    port = int(harness.stdout.readline().strip().rsplit(":", 1)[1])
    connection = http.client.HTTPConnection(controller.HOST, port, timeout=10)
    connection.request("GET", "/hold")
    # The response owns the socket; keep it open while the worker is held.
    held.append(connection.getresponse())
    pgid = int(held[0].readline())
    return port, pgid, 0


def _preloading_server(fifo: int) -> tuple[int, int, int]:
    """Read the held preload's report; return (port, pgid, controller pid)."""
    assert select.select([fifo], [], [], HARNESS_SECONDS)[0]
    _master, controller_pid, pgid, port = json.loads(os.read(fifo, 4096))
    return port, pgid, controller_pid


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _Harness(
                "browser.browser_server_stop_harness",
                ("controller-second-stop",),
                signal.SIGINT,
                signal.SIGTERM,
            ),
            id="controller-second-stop-in-teardown",
        ),
        pytest.param(
            _Harness(
                "browser.browser_server_stop_harness",
                ("controller",),
                signal.SIGKILL,
                signal.SIGKILL,
            ),
            id="controller-killed",
        ),
        pytest.param(
            _Harness(
                "browser.browser_server_stop_harness",
                ("supervisor",),
                signal.SIGKILL,
                signal.SIGKILL,
            ),
            id="start-master-killed",
        ),
        pytest.param(
            _Harness(
                supervisor.__name__,
                ("suite", "patient"),
                signal.SIGKILL,
                signal.SIGKILL,
            ),
            id="shell-supervisor-killed-in-preload",
        ),
    ],
)
def test_ended_harness_takes_its_server_down(case: _Harness, tmp_path: Path) -> None:
    shell_path = case.module == supervisor.__name__
    environment = _harness_environment(tmp_path, preload=shell_path)
    held: list[http.client.HTTPResponse] = []
    os.mkfifo(environment["CPI_FLAKE3_HOLD_FIFO"])
    fifo = os.open(environment["CPI_FLAKE3_HOLD_FIFO"], os.O_RDONLY | os.O_NONBLOCK)
    gone = threading.Event()
    reaper: threading.Thread | None = None
    survivors: tuple[int, int] = (0, 0)
    with (
        _child_subreaper(),
        (tmp_path / "harness.log").open("wb") as harness_log,
        subprocess.Popen(  # noqa: S603 - this interpreter and a fixed module.
            [
                sys.executable,
                "-m",
                case.module,
                *case.mode,
                *(() if shell_path else (str(tmp_path),)),
            ],
            cwd=REPOSITORY,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=harness_log,
            text=True,
        ) as harness,
    ):
        try:
            port, pgid, controller_pid = (
                _preloading_server(fifo) if shell_path else _held_server(harness, held)
            )
            survivors = (controller_pid, pgid)
            harness.send_signal(case.stop)
            harness_exit = harness.wait(timeout=HARNESS_SECONDS)
            # The harness is gone and its orphans are ours: only wait.
            targets = (*((controller_pid,) if controller_pid else ()), -pgid)
            reaper = threading.Thread(target=_reap, args=(targets, gone), daemon=True)
            reaper.start()
            observed = {
                "server_gone": gone.wait(timeout=DEATH_SECONDS),
                "controller_alive": _alive(controller_pid),
                "listening": _listening(port),
                "plain_bind": _plain_bindable(port),
                "harness_exit": harness_exit,
            }
            assert observed == {
                "server_gone": True,
                "controller_alive": False,
                "listening": False,
                "plain_bind": True,
                "harness_exit": -case.exit_signal,
            }
        finally:
            # Only now, and only for a failing run, end what is left.
            os.close(fifo)
            for response in held:
                response.close()
            if harness.returncode is None:
                harness.kill()
                harness.wait(timeout=HARNESS_SECONDS)
            controller_pid, pgid = survivors
            if pgid and not gone.is_set():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
                if controller_pid:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(controller_pid, signal.SIGKILL)
                if reaper is None:
                    _reap((*((controller_pid,) if controller_pid else ()), -pgid), gone)
                else:
                    reaper.join(timeout=HARNESS_SECONDS)
