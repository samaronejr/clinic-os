"""A stop between the server spawn and its stored handle releases the port."""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager, nullcontext
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pytest
from ops.testing import browser_server_controller as controller
from ops.testing import renewal_runner as runner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from types import FrameType

    Tap = Callable[[str | None], object]


class _UnconvertedStopError(Exception):
    """Stands in for SIG_DFL so an unhandled SIGTERM fails instead of killing pytest."""


def _unconverted_stop(_signum: int, _frame: object) -> None:
    raise _UnconvertedStopError


def _unexpected_readiness(*_args: object) -> None:
    pytest.fail("the stop did not unwind the launcher during the handoff")


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
            _Case(_prepare_controller, signal.SIGTERM, _unconverted_stop, SystemExit),
            id="controller-stop",
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
        # A real child inherits the listener and holds it until stdin closes;
        # the test never signals it, so only launcher cleanup can stop it.
        initialize(
            child,
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
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
        with socket.socket() as probe:
            probe.bind((controller.HOST, port))
        assert child.returncode == -signal.SIGTERM
    finally:
        signal.signal(sig, previous)
        for child, _port in launched:
            child.communicate(timeout=10)
