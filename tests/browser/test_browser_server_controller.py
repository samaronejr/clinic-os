from __future__ import annotations

import errno
import http.client
import os
import socket
import subprocess
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from ops.testing import browser_server_controller as controller

if TYPE_CHECKING:
    from pathlib import Path


def test_controller_keeps_port_bound_through_gunicorn_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in controller.REQUIRED_ENVIRONMENT:
        monkeypatch.setenv(name, "synthetic")
    monkeypatch.setenv("CLINIC_BROWSER_EVIDENCE_ROOT", str(tmp_path))
    process = Mock(spec=subprocess.Popen)

    with ExitStack() as server:

        def spawn(
            argv: list[str], *, pass_fds: tuple[int, ...] = (), **kwargs: object
        ) -> Mock:
            bind = argv[argv.index("--bind") + 1]
            if pass_fds:
                (fd,) = pass_fds
                assert bind == f"fd://{fd}"
                listener = server.enter_context(
                    socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
                )
                assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                address = listener.getsockname()
            else:
                host, port = bind.split(":")
                address = (host, int(port))
            with (
                socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competitor,
                pytest.raises(OSError, match=r"\[Errno 98\]") as raised,
            ):
                competitor.bind(address)
            assert raised.value.errno == errno.EADDRINUSE
            assert pass_fds
            return process

        def ready(port: int, *args: object) -> None:
            with (
                socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competitor,
                pytest.raises(OSError, match=r"\[Errno 98\]") as raised,
            ):
                competitor.bind((controller.HOST, port))
            assert raised.value.errno == errno.EADDRINUSE

        monkeypatch.setattr(subprocess, "Popen", spawn)
        monkeypatch.setattr(controller, "wait_until_ready", ready)
        monkeypatch.setattr(controller, "stop_server", lambda _: server.close())
        monkeypatch.setattr(controller, "build_suite", lambda *_: dict)
        monkeypatch.setattr(controller, "export_artifacts", lambda *_: {})

        assert controller.run_suite("patient", ["patient"]) == os.EX_OK


def test_readiness_rejects_a_dead_master_before_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = 3
    server_log = tmp_path / "server.log"
    server_log.write_text("synthetic boot error", encoding="utf-8")
    probe = Mock()
    monkeypatch.setattr(http.client, "HTTPConnection", probe)

    with pytest.raises(
        controller.BrowserServerError,
        match=r"server exited with status 3.*synthetic boot error",
    ):
        controller.wait_until_ready(58419, server_log, process)

    probe.assert_not_called()
