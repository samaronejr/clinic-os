"""Run one real browser-server launcher as its own harness process.

The cancellation tests stop or kill this process; the launch, readiness,
teardown and signal dispositions are production code. Only the WSGI app is
a stub and the suite body waits for the test's stop.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from ops.testing import browser_server_controller as controller
from ops.testing import browser_server_supervisor as supervisor

STUB_APP = "cpi_flake3_stop_app:application"


def _with_stub_app(argv: list[str]) -> list[str]:
    return [*argv[:-1], STUB_APP]


def _announce_then_wait(base_url: str) -> dict[str, bytes]:
    sys.stdout.write(f"{base_url}\n")
    sys.stdout.flush()
    signal.pause()
    return {}


def _run_controller(monkeypatch: pytest.MonkeyPatch, *, second_stop: bool) -> None:
    spawn = subprocess.Popen

    def spawn_stub(argv: list[str], **kwargs: object) -> object:
        return spawn(_with_stub_app(argv), **cast("Any", kwargs))

    monkeypatch.setattr(subprocess, "Popen", spawn_stub)
    monkeypatch.setattr(
        controller,
        "build_suite",
        lambda _suite, base_url: lambda: _announce_then_wait(base_url),
    )
    if second_stop:
        terminate = supervisor.SupervisedMaster.terminate

        def stop_again_then_terminate(master: supervisor.SupervisedMaster) -> None:
            # A normal stop request arrives as teardown starts.
            os.kill(os.getpid(), signal.SIGTERM)
            terminate(master)

        monkeypatch.setattr(
            supervisor.SupervisedMaster, "terminate", stop_again_then_terminate
        )
    monkeypatch.setattr(sys, "argv", ["browser_server_controller", "suite", "patient"])
    controller.main()


def _run_supervisor(root: Path) -> None:
    owner: list[supervisor.SupervisedMaster] = []
    with controller.reserve_port() as (port, listener_fd):
        supervisor.start_master(
            _with_stub_app(
                supervisor.supervised_argv(
                    Path(sys.executable), listener_fd, root / "access.log"
                )
            ),
            dict(os.environ),
            root / "server.log",
            owner=owner,
            pass_fds=(listener_fd,),
        )
    controller.wait_until_ready(port, root / "server.log")
    _announce_then_wait(f"http://{controller.HOST}:{port}")


def main() -> None:
    mode, root = sys.argv[1], Path(sys.argv[2])
    with pytest.MonkeyPatch.context() as monkeypatch:
        if mode == "supervisor":
            _run_supervisor(root)
        else:
            _run_controller(monkeypatch, second_stop=mode == "controller-second-stop")


if __name__ == "__main__":
    main()
