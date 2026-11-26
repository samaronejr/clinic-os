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
import inspect
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
    from contextlib import AbstractContextManager
    from types import CodeType, FrameType

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
    """Report whether a TCP socket listens on the server's own 127.0.0.1:port.

    Only that exact endpoint: listeners on other addresses (127.0.0.2,
    IPv6) do not serve it and do not stop the server's restart.
    """
    endpoint = f"0100007F:{port:04X}"
    rows = Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]
    return any(
        row.split()[1] == endpoint and row.split()[3] == TCP_LISTEN for row in rows
    )


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


def _prepare_renewal_without_prctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tap: Tap
) -> Callable[[], object]:
    # Missing prctl must cost only parent-death, never the stop mask.
    monkeypatch.setattr(supervisor, "_prctl", _without_prctl)
    _reset_capabilities()
    return _prepare_renewal(tmp_path, monkeypatch, tap)


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
        pytest.param(
            _Case(
                _prepare_renewal_without_prctl,
                signal.SIGTERM,
                runner._interrupted,
                runner.RenewalInterruptError,
            ),
            id="renewal-stop-without-prctl",
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
        monkeypatch.undo()
        _reset_capabilities()


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

    fired: list[int] = []

    def alarm_before_restore(
        how: int, mask: tuple[int, ...] | frozenset[int]
    ) -> set[int]:
        if how == signal.SIG_SETMASK and not fired:
            # One caller alarm fires just before the restoring syscall.
            fired.append(how)
            signal.raise_signal(signal.SIGALRM)
        return real_sigmask(how, mask)

    previous_alarm = signal.signal(signal.SIGALRM, _alarm)
    monkeypatch.setattr(signal, "pthread_sigmask", alarm_before_restore)
    try:
        # The executable is missing: the spawn fails inside the masked region.
        with pytest.raises(_AlarmError):
            supervisor.start_master(
                [str(tmp_path / "missing")], {}, tmp_path / "spawn.log", owner=[]
            )
        observed = {"mask_kept": set(real_sigmask(signal.SIG_BLOCK, ())) == mask_before}
        assert observed == {"mask_kept": True}
    finally:
        real_sigmask(signal.SIG_SETMASK, mask_before)
        signal.signal(signal.SIGALRM, previous_alarm)


# A one-shot HTTP server on the inherited listener: proof that serving works.
ONE_SHOT_SERVER = (
    "import socket, sys\n"
    "listener = socket.socket(fileno=int(sys.argv[1]))\n"
    "client, _ = listener.accept()\n"
    "client.recv(4096)\n"
    "client.sendall(b'HTTP/1.0 200 OK\\r\\nContent-Length: 0\\r\\n\\r\\n')\n"
    "client.close()\n"
)


def _without_prctl() -> Callable[..., int]:
    raise AttributeError(name="prctl")


def _reset_capabilities() -> None:
    # Older revisions had other (or no) cached checks.
    for name in ("process_controls_available", "parent_death_available"):
        cached = getattr(supervisor, name, None)
        if cached is not None:
            cached.cache_clear()


@pytest.mark.parametrize("missing", ["pthread_sigmask", "valid_signals", "prctl"])
def test_missing_lifetime_control_falls_back_and_still_serves(
    missing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if missing == "prctl":
        monkeypatch.setattr(supervisor, "_prctl", _without_prctl)
    else:
        monkeypatch.delattr(signal, missing)
    _reset_capabilities()
    owner: list[supervisor.SupervisedMaster] = []
    statuses: list[int] = []
    raised: list[str] = []
    try:
        with controller.reserve_port() as (port, listener_fd):
            try:
                supervisor.start_master(
                    [sys.executable, "-c", ONE_SHOT_SERVER, str(listener_fd)],
                    dict(os.environ),
                    tmp_path / "fallback.log",
                    owner=owner,
                    pass_fds=(listener_fd,),
                )
            except Exception as error:  # noqa: BLE001 - recorded as the outcome
                raised.append(type(error).__name__)
        if owner:
            connection = http.client.HTTPConnection(controller.HOST, port, timeout=10)
            try:
                connection.request("GET", "/")
                statuses.append(connection.getresponse().status)
            finally:
                connection.close()
        observed = {"raised": raised, "statuses": statuses, "owned": len(owner)}
        assert observed == {"raised": [], "statuses": [200], "owned": 1}
    finally:
        for master in owner:
            master.terminate()
        monkeypatch.undo()
        _reset_capabilities()


def test_missing_libc_prctl_still_imports_and_reports_unavailable() -> None:
    probe = (
        "import ctypes\n"
        "class NoPrctl(ctypes.CDLL):\n"
        "    def __getattr__(self, name):\n"
        "        if name == 'prctl':\n"
        "            raise AttributeError(name)\n"
        "        return super().__getattr__(name)\n"
        "ctypes.CDLL = NoPrctl\n"
        "from ops.testing import browser_server_supervisor as supervisor\n"
        "print('imported', supervisor.parent_death_available())\n"
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
        ["imported False"],
    )


class _DeadlineError(Exception):
    """The caller's own deadline handler (SIGALRM)."""


def _deadline(_signum: int, _frame: object) -> None:
    raise _DeadlineError


def test_caller_deadline_interrupts_a_blocking_log_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The log is a FIFO with no reader, so opening it blocks until one comes.
    log_fifo = tmp_path / "server.log"
    os.mkfifo(log_fifo)
    release = threading.Event()
    spawned: list[list[str]] = []
    spawn = subprocess.Popen

    path_open = cast("Any", type(log_fifo).open)

    def deadline_at_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == log_fifo:
            # The caller's deadline fires as the launcher starts the open.
            signal.raise_signal(signal.SIGALRM)
        return path_open(path, *args, **kwargs)

    def unblock_a_red_run() -> None:
        # Bounded: only a launcher that deferred the deadline is still
        # blocked in open() when this fires; then it is let through.
        if not release.wait(timeout=10):
            with contextlib.suppress(OSError):
                os.close(os.open(log_fifo, os.O_RDONLY | os.O_NONBLOCK))

    def record_spawn(argv: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        spawned.append(argv)
        return spawn(argv, **cast("Any", kwargs))

    monkeypatch.setattr(type(log_fifo), "open", deadline_at_open)
    monkeypatch.setattr(subprocess, "Popen", record_spawn)
    previous = signal.signal(signal.SIGALRM, _deadline)
    releaser = threading.Thread(target=unblock_a_red_run, daemon=True)
    releaser.start()
    mask_before = _caller_mask()
    try:
        with pytest.raises(_DeadlineError):
            supervisor.start_master(
                [sys.executable, "-c", "pass"],
                {},
                log_fifo,
                owner=[],
            )
        observed = {"spawned": spawned, "mask_kept": _caller_mask() == mask_before}
        assert observed == {"spawned": [], "mask_kept": True}
    finally:
        signal.signal(signal.SIGALRM, previous)
        release.set()
        releaser.join(timeout=15)


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
    _end_harness(
        case,
        tmp_path,
        {
            "server_gone": True,
            "controller_alive": False,
            "listening": False,
            "plain_bind": True,
            "harness_exit": -case.exit_signal,
        },
    )


def _end_harness(case: _Harness, tmp_path: Path, expected: dict[str, object]) -> None:
    """Start the harness, end it by ``case.stop`` and assert what is left.

    Everything acquired here is released by the ExitStack, also when setup
    fails; the assertion runs inside it, before a failing run's survivors
    are ended.
    """
    shell_path = case.module == supervisor.__name__
    environment = _harness_environment(tmp_path, preload=shell_path)
    gone = threading.Event()
    survivors: list[int] = []
    with contextlib.ExitStack() as stack:
        before = _children()
        os.mkfifo(environment["CPI_FLAKE3_HOLD_FIFO"])
        fifo = os.open(environment["CPI_FLAKE3_HOLD_FIFO"], os.O_RDONLY | os.O_NONBLOCK)
        stack.callback(os.close, fifo)
        stack.enter_context(_child_subreaper())
        harness_log = stack.enter_context((tmp_path / "harness.log").open("wb"))
        harness = stack.enter_context(
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
            )
        )
        stack.callback(_end_survivors, harness, survivors, gone, before)
        held: list[http.client.HTTPResponse] = []
        stack.callback(_close_all, held)
        port, pgid, controller_pid = (
            _preloading_server(fifo) if shell_path else _held_server(harness, held)
        )
        survivors.extend((controller_pid, pgid))
        harness.send_signal(case.stop)
        harness_exit = harness.wait(timeout=HARNESS_SECONDS)
        # The harness is gone and its orphans are ours: only wait.
        threading.Thread(
            target=_reap, args=(_targets(survivors), gone), daemon=True
        ).start()
        observed = {
            "server_gone": gone.wait(timeout=DEATH_SECONDS),
            "controller_alive": _alive(controller_pid),
            "listening": _listening(port),
            "plain_bind": _plain_bindable(port),
            "harness_exit": harness_exit,
        }
        assert observed == expected


def _foreign_group(pgid: int) -> bool:
    """Only a known, positive group other than this runner's may be signalled."""
    return pgid > 0 and pgid != os.getpgrp()


def _targets(survivors: list[int]) -> tuple[int, ...]:
    controller_pid, pgid = survivors
    return (
        *((controller_pid,) if controller_pid else ()),
        *((-pgid,) if _foreign_group(pgid) else ()),
    )


def _close_all(responses: list[http.client.HTTPResponse]) -> None:
    for response in responses:
        response.close()


def _children() -> set[int]:
    """Every current child of this process, across its threads."""
    pids: set[int] = set()
    for task in Path("/proc/self/task").iterdir():
        with contextlib.suppress(OSError):
            pids.update(int(pid) for pid in (task / "children").read_text().split())
    return pids


def _end_survivors(
    harness: subprocess.Popen[str],
    survivors: list[int],
    gone: threading.Event,
    before: frozenset[int] | set[int] = frozenset(),
) -> None:
    """After the assertion, end and reap whatever a failing run left.

    Covers every child adopted since ``before`` was taken, whatever the run
    managed to learn about it, plus the recorded controller and master, by
    pid only. Each is killed and reaped here (bounded join), never by
    waiting on a reaper that an interrupted run may not have started.
    """
    if harness.returncode is None:
        harness.kill()
        harness.wait(timeout=HARNESS_SECONDS)
    # PID-only: signal and reap exactly the processes this test spawned or
    # adopted, never a process group (it may hold processes the test does
    # not own). The master's pid is its recorded group id.
    pending = [pid for pid in survivors if pid > 0] if not gone.is_set() else []
    handled: set[int] = set()
    # One pass per tree level (shell supervisor, controller, master,
    # workers): a grandchild is adopted only once its parent is reaped.
    for _level in range(4):
        targets = [*pending, *(_children() - set(before) - {harness.pid} - handled)]
        pending = []
        if not targets:
            return
        handled.update(targets)
        for pid in targets:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        reaper = threading.Thread(
            target=_reap, args=(tuple(targets), threading.Event()), daemon=True
        )
        reaper.start()
        reaper.join(timeout=HARNESS_SECONDS)


def test_cleanup_reaps_adopted_children_without_a_running_reaper() -> None:
    # A run interrupted after the harness exited but before its reaper began:
    # the adopted server group must still be ended and reaped by cleanup.
    spawner = (
        "import signal, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c',"
        " 'import signal; signal.pause()'], start_new_session=True)\n"
        "print(child.pid, flush=True)\n"
        "signal.pause()\n"
    )
    leftovers: list[int] = []
    with (
        _child_subreaper(),
        subprocess.Popen(  # noqa: S603 - this interpreter, fixed probe.
            [sys.executable, "-c", spawner], stdout=subprocess.PIPE, text=True
        ) as harness,
    ):
        assert harness.stdout is not None
        assert select.select([harness.stdout], [], [], HARNESS_SECONDS)[0]
        orphan = int(harness.stdout.readline())
        harness.kill()
        harness.wait(timeout=HARNESS_SECONDS)
        try:
            _end_survivors(harness, [0, orphan], threading.Event())
            try:
                reaped, _status = os.waitpid(orphan, os.WNOHANG)
            except ChildProcessError:
                reaped = -1
            if reaped == orphan:
                leftovers.append(orphan)
            observed = {"left_unreaped": leftovers, "alive": _alive(orphan)}
            assert observed == {"left_unreaped": [], "alive": False}
        finally:
            if _alive(orphan):
                os.kill(orphan, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(orphan, 0)


def test_failed_harness_start_leaves_no_test_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EAGAIN, "spawn refused")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    case = _Harness(
        supervisor.__name__, ("suite", "patient"), signal.SIGKILL, signal.SIGKILL
    )
    with pytest.raises(OSError, match="spawn refused"):
        _end_harness(case, tmp_path, {})
    fifo = str(tmp_path / "hold.fifo")
    open_fifos = [
        entry.name for entry in Path("/proc/self/fd").iterdir() if _link(entry) == fifo
    ]
    assert open_fifos == []


# The fixture answers /hold with an empty 503: setup fails before it learns
# the server's process group.
REFUSING_HOLD_SOURCE = (
    "def application(environ, start_response):\n"
    "    if environ['PATH_INFO'] == '/hold':\n"
    "        start_response('503 Service Unavailable', [('Content-Length', '0')])\n"
    "        return [b'']\n"
    "    start_response('200 OK', [('Content-Length', '0')])\n"
    "    return [b'']\n"
)


def test_failed_hold_leaves_no_adopted_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "STUB_APP_SOURCE", REFUSING_HOLD_SOURCE)
    case = _Harness(
        "browser.browser_server_stop_harness",
        ("controller",),
        signal.SIGKILL,
        signal.SIGKILL,
    )
    before = _children()
    try:
        with pytest.raises(ValueError, match="invalid literal"):
            _end_harness(case, tmp_path, {})
        left = _children() - before
        observed = {"adopted_children_left": sorted(left)}
        assert observed == {"adopted_children_left": []}
    finally:
        for pid in _children() - before:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)


def test_failed_preload_report_leaves_runner_alive_and_nothing_unreaped(
    tmp_path: Path,
) -> None:
    # A separate runner process (own session, so nothing it does can reach
    # this one) starts the real shell supervisor; the held preload exists but
    # the report fails before the group is known. Its cleanup must not signal
    # its own group, and must leave no child unreaped.
    runner_source = (
        "import signal, sys\n"
        "from pathlib import Path\n"
        "from browser import test_browser_server_cancellation as module\n"
        "real = module._preloading_server\n"
        "def failing(fifo):\n"
        "    real(fifo)\n"
        "    raise RuntimeError('preload report failed before its group')\n"
        "module._preloading_server = failing\n"
        "case = module._Harness(module.supervisor.__name__, ('suite', 'patient'),\n"
        "                       signal.SIGKILL, signal.SIGKILL)\n"
        "before = module._children()\n"
        "try:\n"
        "    module._end_harness(case, Path(sys.argv[1]), {})\n"
        "except RuntimeError:\n"
        "    pass\n"
        "print(len(module._children() - before), flush=True)\n"
    )
    with (
        _child_subreaper(),
        subprocess.Popen(  # noqa: S603 - this interpreter, fixed probe.
            [sys.executable, "-c", runner_source, str(tmp_path)],
            cwd=REPOSITORY,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (str(REPOSITORY), str(REPOSITORY / "tests"))
                ),
            },
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as runner_process,
    ):
        before = _children()
        try:
            output, _ = runner_process.communicate(timeout=HARNESS_SECONDS * 2)
            observed = {
                "runner_exit": runner_process.returncode,
                "children_left_in_runner": output.strip(),
            }
            assert observed == {"runner_exit": 0, "children_left_in_runner": "0"}
        finally:
            # Anything a failing runner left was adopted here; end it by pid.
            for pid in _children() - before - {runner_process.pid}:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)


def test_cleanup_spares_an_unrelated_process_in_an_adopted_childs_group() -> None:
    # An unrelated process already running before the test leads a group;
    # a test-owned grandchild joins that group and is adopted. Cleanup must
    # reap the test-owned child and leave the unrelated process alone.
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"], process_group=0
    )
    spawner = (
        "import signal, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c',"
        " 'import signal; signal.pause()'], process_group=int(sys.argv[1]))\n"
        "print(child.pid, flush=True)\n"
        "signal.pause()\n"
    )
    try:
        with (
            _child_subreaper(),
            subprocess.Popen(  # noqa: S603 - this interpreter, fixed probe.
                [sys.executable, "-c", spawner, str(unrelated.pid)],
                stdout=subprocess.PIPE,
                text=True,
            ) as harness,
        ):
            before = _children()
            assert harness.stdout is not None
            assert select.select([harness.stdout], [], [], HARNESS_SECONDS)[0]
            owned = int(harness.stdout.readline())
            harness.kill()
            harness.wait(timeout=HARNESS_SECONDS)
            _end_survivors(harness, [], threading.Event(), before)
            observed = {
                "owned_alive": _alive(owned),
                "owned_unreaped": owned in _children(),
                "unrelated_alive": unrelated.poll() is None,
            }
            assert observed == {
                "owned_alive": False,
                "owned_unreaped": False,
                "unrelated_alive": True,
            }
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=HARNESS_SECONDS)


def _link(entry: Path) -> str:
    try:
        return str(entry.readlink())
    except OSError:
        return ""


def test_release_witness_checks_only_the_servers_address() -> None:
    with socket.socket() as own, socket.socket() as other:
        # Hold 127.0.0.1:port (bound, not listening), then listen on the same
        # port at another address: that listener does not serve 127.0.0.1.
        own.bind((controller.HOST, 0))
        port = own.getsockname()[1]
        other.bind(("127.0.0.2", port))
        other.listen()
        observed = {"other_address_only": _listening(port)}
        own.listen()
        observed["own_listening"] = _listening(port)
    assert observed == {"other_address_only": False, "own_listening": True}


class _AlarmInTeardownError(Exception):
    """The caller's deadline handler firing during teardown."""


def _alarm_in_teardown(_signum: int, _frame: object) -> None:
    raise _AlarmInTeardownError


def _teardown_first_line() -> int:
    """Line of the first statement (not a comment) in ``run_suite``'s finally."""
    lines, start = inspect.getsourcelines(controller.run_suite)
    at = next(index for index, line in enumerate(lines) if line.strip() == "finally:")
    following = next(
        index
        for index in range(at + 1, len(lines))
        if lines[index].strip() and not lines[index].strip().startswith("#")
    )
    return start + following


@contextmanager
def _signal_at_line(
    code: CodeType, line: int, sig: signal.Signals
) -> Iterator[list[int]]:
    """Deliver ``sig`` once, by a real kernel signal, as ``line`` starts."""
    monitoring = sys.monitoring
    tool = next(
        candidate for candidate in range(6) if monitoring.get_tool(candidate) is None
    )
    monitoring.use_tool_id(tool, "cpi-flake3-teardown-cut")
    fired: list[int] = []

    def at_line(_code: CodeType, number: int) -> object:
        if number == line and not fired:
            fired.append(number)
            signal.raise_signal(sig)
        return None

    monitoring.register_callback(tool, monitoring.events.LINE, at_line)
    monitoring.set_local_events(tool, code, monitoring.events.LINE)
    try:
        yield fired
    finally:
        monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)


def _teardown_cut(  # noqa: PLR0913 - one cut: where, which signal, and its records.
    where: str,
    second: signal.Signals,
    fifo: int,
    server: list[int],
    teardowns: list[supervisor.SupervisedMaster],
    spawned: list[supervisor.SupervisedMaster],
    monkeypatch: pytest.MonkeyPatch,
) -> AbstractContextManager[list[int]]:
    """Patch in the body Ctrl-C and the second signal at ``where``.

    The context yields a non-empty list once the second signal was sent.
    """
    terminate = supervisor.SupervisedMaster.terminate
    launch = cast("Any", supervisor.start_master)
    real_sigmask = signal.pthread_sigmask
    fired: list[int] = []

    def record_master(*args: object, **kwargs: object) -> supervisor.SupervisedMaster:
        master: supervisor.SupervisedMaster = launch(*args, **kwargs)
        spawned.append(master)
        return master

    def ctrl_c_once_preloading(_port: int, _log: Path) -> None:
        server.extend(_preloading_server(fifo)[:2])
        if where == "never-ready":
            # The server never becomes ready: readiness fails as in production.
            fired.append(0)
            controller._fail("readiness never reached (no attempt)")
        signal.raise_signal(signal.SIGINT)

    def signal_then_terminate(master: supervisor.SupervisedMaster) -> None:
        teardowns.append(master)
        if where == "terminate-entry":
            fired.append(second)
            signal.raise_signal(second)
        terminate(master)

    def signal_before_first_restore(
        how: int, mask: tuple[int, ...] | frozenset[int] | set[int]
    ) -> set[int]:
        if how == signal.SIG_SETMASK and teardowns and not fired:
            fired.append(how)
            signal.raise_signal(second)
        return set(real_sigmask(how, mask))

    monkeypatch.setattr(controller, "start_master", record_master)
    monkeypatch.setattr(controller, "wait_until_ready", ctrl_c_once_preloading)
    monkeypatch.setattr(supervisor.SupervisedMaster, "terminate", signal_then_terminate)
    if where == "final-restore":
        monkeypatch.setattr(signal, "pthread_sigmask", signal_before_first_restore)
    if where == "first-line":
        return _signal_at_line(
            controller.run_suite.__code__, _teardown_first_line(), second
        )
    return nullcontext(fired)


@pytest.mark.parametrize(
    ("where", "second"),
    [
        pytest.param("terminate-entry", signal.SIGINT, id="ctrl-c-at-terminate"),
        pytest.param("terminate-entry", signal.SIGALRM, id="alarm-at-terminate"),
        pytest.param("final-restore", signal.SIGALRM, id="alarm-at-mask-restore"),
        pytest.param("first-line", signal.SIGINT, id="ctrl-c-at-teardown-start"),
        pytest.param("never-ready", signal.SIGINT, id="server-never-ready"),
    ],
)
def test_caught_repeated_stops_in_teardown_still_end_the_server(
    where: str,
    second: signal.Signals,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real controller, the real config.wsgi preload held in the master, a
    # caller that catches the unwind: one Ctrl-C in the body, then a second
    # signal in the production teardown (at its start, at every entry to
    # terminate, or as the caller's mask is restored).
    environment = _harness_environment(tmp_path, preload=True)
    for name in ("DJANGO_SETTINGS_MODULE", "CPI_FLAKE3_HOLD_FIFO", "PYTHONPATH"):
        monkeypatch.setenv(name, environment[name])
    for name in controller.REQUIRED_ENVIRONMENT:
        monkeypatch.setenv(name, environment[name])
    monkeypatch.setattr(sys, "argv", ["browser_server_controller", "suite", "patient"])
    os.mkfifo(environment["CPI_FLAKE3_HOLD_FIFO"])
    fifo = os.open(environment["CPI_FLAKE3_HOLD_FIFO"], os.O_RDONLY | os.O_NONBLOCK)
    server: list[int] = []
    teardowns: list[supervisor.SupervisedMaster] = []
    real_sigmask = signal.pthread_sigmask
    previous_int = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_alarm = signal.signal(signal.SIGALRM, _alarm_in_teardown)
    mask_before = set(real_sigmask(signal.SIG_BLOCK, ()))
    spawned: list[supervisor.SupervisedMaster] = []
    cut = _teardown_cut(where, second, fifo, server, teardowns, spawned, monkeypatch)
    try:
        unwinds = (
            KeyboardInterrupt,
            _AlarmInTeardownError,
            controller.BrowserServerError,
        )
        with cut as fired, pytest.raises(unwinds):
            controller.main()
        port, pgid = server
        observed = {
            "second_signal_sent": bool(fired),
            "server_alive": _alive(pgid),
            "listening": _listening(port),
            "plain_bind": _plain_bindable(port),
            "mask_kept": set(real_sigmask(signal.SIG_BLOCK, ())) == mask_before,
        }
        assert observed == {
            "second_signal_sent": True,
            "server_alive": False,
            "listening": False,
            "plain_bind": True,
            "mask_kept": True,
        }
    finally:
        real_sigmask(signal.SIG_SETMASK, mask_before)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGALRM, previous_alarm)
        os.close(fifo)
        # A failing run's server is ended through its own handle, afterwards.
        for master in spawned:
            if master.process.poll() is None:
                master.process.kill()
                master.process.wait(timeout=HARNESS_SECONDS)


@contextmanager
def _signal_at_start(code: CodeType, sig: signal.Signals) -> Iterator[list[int]]:
    """Deliver ``sig`` once, by a real kernel signal, as ``code`` starts."""
    monitoring = sys.monitoring
    tool = next(
        candidate for candidate in range(6) if monitoring.get_tool(candidate) is None
    )
    monitoring.use_tool_id(tool, "cpi-flake3-teardown-entry")
    fired: list[int] = []

    def at_start(_code: CodeType, _offset: int) -> object:
        if not fired:
            fired.append(sig)
            signal.raise_signal(sig)
        return None

    monitoring.register_callback(tool, monitoring.events.PY_START, at_start)
    monitoring.set_local_events(tool, code, monitoring.events.PY_START)
    try:
        yield fired
    finally:
        monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_renewal_second_stop_at_teardown_entry_still_ends_the_server(
    sig: signal.Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real renewal suite runner with a real config.wsgi preload held in
    # the published master: a stop in the body, then the same stop as the
    # renewal teardown routine starts; the caller catches the unwind.
    environment = _harness_environment(tmp_path, preload=True)
    run = _prepare_renewal(tmp_path, monkeypatch, lambda _url: nullcontext([]))
    server_env: dict[str, str] = {
        key: environment[key]
        for key in (
            "PATH",
            "HOME",
            "PYTHONPATH",
            "DJANGO_SETTINGS_MODULE",
            "CPI_FLAKE3_HOLD_FIFO",
            "XDG_RUNTIME_DIR",
        )
        if key in environment
    }
    monkeypatch.setattr(
        runner,
        "_server_environment",
        lambda _: {**server_env, "REALTIME_REDIS_URL": "synthetic"},
    )
    os.mkfifo(environment["CPI_FLAKE3_HOLD_FIFO"])
    fifo = os.open(environment["CPI_FLAKE3_HOLD_FIFO"], os.O_RDONLY | os.O_NONBLOCK)
    server: list[int] = []
    spawned: list[supervisor.SupervisedMaster] = []
    launch = cast("Any", supervisor.start_master)

    def record_master(*args: object, **kwargs: object) -> supervisor.SupervisedMaster:
        master: supervisor.SupervisedMaster = launch(*args, **kwargs)
        spawned.append(master)
        return master

    def stop_once_preloading(_port: int, _log: Path) -> None:
        server.extend(_preloading_server(fifo)[:2])
        signal.raise_signal(sig)

    monkeypatch.setattr(runner, "start_master", record_master)
    monkeypatch.setattr(runner, "wait_until_ready", stop_once_preloading)
    teardown = getattr(runner, "_terminate_master", supervisor.end_recorded)
    previous = signal.signal(sig, runner._interrupted)
    mask_before = _caller_mask()
    try:
        with (
            _signal_at_start(teardown.__code__, sig) as fired,
            pytest.raises(runner.RenewalInterruptError),
        ):
            run()
        port, pgid = server
        observed = {
            "second_stop_sent": bool(fired),
            "server_alive": _alive(pgid),
            "listening": _listening(port),
            "plain_bind": _plain_bindable(port),
            "mask_kept": _caller_mask() == mask_before,
        }
        assert observed == {
            "second_stop_sent": True,
            "server_alive": False,
            "listening": False,
            "plain_bind": True,
            "mask_kept": True,
        }
    finally:
        signal.signal(sig, previous)
        os.close(fifo)
        # A failing run's server is ended through its own handle, afterwards.
        for master in spawned:
            if master.process.poll() is None:
                master.process.kill()
                master.process.wait(timeout=HARNESS_SECONDS)
