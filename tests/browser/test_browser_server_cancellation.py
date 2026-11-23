"""A stop or kill of the harness at any point releases the server's port."""

from __future__ import annotations

import contextlib
import ctypes
import http.client
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
HARNESS_SECONDS = 60
# Answers with the server's process group; /hold then keeps its worker busy
# for good, so only a signal (not Gunicorn's idle parent check) ends it. Its
# connection lingers 0 s, so the worker's death resets it synchronously
# instead of leaving a FIN handshake in flight at the port check.
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


def _unexpected_readiness(*_args: object) -> None:
    pytest.fail("the stop did not unwind the launcher during the handoff")


def _caller_mask() -> set[int]:
    return set(signal.pthread_sigmask(signal.SIG_BLOCK, ()))


def _assert_released(
    child: subprocess.Popen[bytes], port: int, mask_before: set[int]
) -> None:
    # Launcher cleanup alone: reaped by its own wait, port free, mask kept.
    assert child.returncode == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
    with socket.socket() as probe:
        probe.bind((controller.HOST, port))
    assert _caller_mask() == mask_before


def _assert_rebindable(port: int) -> None:
    # Answered requests leave TIME_WAIT entries, so bind as a restarting
    # server does (Gunicorn sets SO_REUSEADDR); Linux still refuses this
    # while any socket listens on the port.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((controller.HOST, port))
        probe.listen()


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
        _assert_released(*launched[0], mask_before)
    finally:
        signal.signal(sig, previous)
        for child, _port in launched:
            if child.returncode is None:
                child.kill()
                child.wait(timeout=10)


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
        assert real_sigmask(signal.SIG_BLOCK, ()) == mask_before
        assert spawned == []
    finally:
        real_sigmask(signal.SIG_SETMASK, mask_before)


_PRCTL = ctypes.CDLL(None, use_errno=True).prctl
_PRCTL.argtypes = [ctypes.c_int, *[ctypes.c_ulong] * 4]
_PRCTL.restype = ctypes.c_int


@contextmanager
def _child_subreaper() -> Iterator[None]:
    """Adopt orphaned descendants so the test can wait for the server group."""
    assert _PRCTL(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        _PRCTL(PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0)


def _reap_group(pgid: int, done: threading.Event) -> None:
    """Reap every adopted member of the group; ECHILD means none is left."""
    try:
        while True:
            os.waitpid(-pgid, 0)
    except ChildProcessError:
        done.set()


@pytest.mark.parametrize(
    ("mode", "stop", "exit_signal"),
    [
        pytest.param(
            "controller-second-stop",
            signal.SIGINT,
            signal.SIGTERM,
            id="controller-second-stop-in-teardown",
        ),
        pytest.param(
            "controller", signal.SIGKILL, signal.SIGKILL, id="controller-killed"
        ),
        pytest.param(
            "supervisor", signal.SIGKILL, signal.SIGKILL, id="start-master-killed"
        ),
    ],
)
def test_ended_harness_takes_its_server_down(
    mode: str, stop: signal.Signals, exit_signal: signal.Signals, tmp_path: Path
) -> None:
    (tmp_path / "cpi_flake3_stop_app.py").write_text(STUB_APP_SOURCE, encoding="utf-8")
    environment = {
        **os.environ,
        **dict.fromkeys(controller.REQUIRED_ENVIRONMENT, "synthetic"),
        "CLINIC_BROWSER_EVIDENCE_ROOT": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(
            (str(tmp_path), str(REPOSITORY), str(REPOSITORY / "tests"))
        ),
        # Gunicorn's control socket stays inside this test's directory.
        "XDG_RUNTIME_DIR": str(tmp_path),
    }
    reaped = threading.Event()
    reaper = threading.Thread(target=_reap_group, args=(0, reaped))
    held = http.client.HTTPConnection(controller.HOST, 0)
    pgid = 0
    with (
        _child_subreaper(),
        (tmp_path / "harness.log").open("wb") as harness_log,
        subprocess.Popen(  # noqa: S603 - this interpreter and a fixed test module.
            [
                sys.executable,
                "-m",
                "browser.browser_server_stop_harness",
                mode,
                str(tmp_path),
            ],
            cwd=REPOSITORY,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=harness_log,
            text=True,
        ) as harness,
    ):
        assert harness.stdout is not None
        try:
            assert select.select([harness.stdout], [], [], HARNESS_SECONDS)[0]
            base_url = harness.stdout.readline().strip()
            port = int(base_url.rsplit(":", 1)[1])
            held = http.client.HTTPConnection(controller.HOST, port, timeout=10)
            held.request("GET", "/hold")
            # A worker is now inside the request and will not return.
            pgid = int(held.getresponse().readline())

            harness.send_signal(stop)
            harness_exit = harness.wait(timeout=HARNESS_SECONDS)
            # The harness is gone; its orphans are ours. Nothing is signalled.
            reaper = threading.Thread(target=_reap_group, args=(pgid, reaped))
            reaper.start()
            assert reaped.wait(timeout=HARNESS_SECONDS), "server group outlived"
            with pytest.raises(ProcessLookupError):
                os.killpg(pgid, 0)
            _assert_rebindable(port)
            # The harness itself ended by that signal, without unwinding.
            assert harness_exit == -exit_signal
        finally:
            # Rescue only after the assertions, for a failing run.
            held.close()
            if harness.returncode is None:
                harness.kill()
                harness.wait(timeout=HARNESS_SECONDS)
            if pgid and not reaped.is_set():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
                if not reaper.is_alive():
                    reaper = threading.Thread(target=_reap_group, args=(pgid, reaped))
                    reaper.start()
                reaper.join(timeout=HARNESS_SECONDS)
