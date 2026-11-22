from __future__ import annotations

import errno
import http.client
import os
import socket
import subprocess
import sys
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest
from ops.testing import browser_server_controller as controller
from ops.testing import browser_server_supervisor as supervisor

if TYPE_CHECKING:
    from pathlib import Path


def test_controller_keeps_port_bound_through_gunicorn_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in controller.REQUIRED_ENVIRONMENT:
        monkeypatch.setenv(name, "synthetic")
    monkeypatch.setenv("CLINIC_BROWSER_EVIDENCE_ROOT", str(tmp_path))
    process = Mock(spec=subprocess.Popen, pid=58419)

    with ExitStack() as server:

        def spawn(
            argv: list[str], *, pass_fds: tuple[int, ...] = (), **kwargs: object
        ) -> Mock:
            bind = argv[argv.index("--bind") + 1]
            if pass_fds:
                fd = int(bind.removeprefix("fd://"))
                assert fd in pass_fds
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
        monkeypatch.setattr(supervisor, "_terminate_group", lambda *_: server.close())
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

    with (
        pytest.raises(
            controller.BrowserServerError, match="server exited with status 3"
        ) as raised,
        controller.with_server_log(server_log),
    ):
        controller.wait_until_ready(58419, process, server_log, "synthetic")

    assert raised.value.__notes__ == ["server log tail: synthetic boot error"]
    probe.assert_not_called()


@pytest.fixture
def controller_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in controller.REQUIRED_ENVIRONMENT:
        monkeypatch.setenv(name, "synthetic")
    monkeypatch.setenv("CLINIC_BROWSER_EVIDENCE_ROOT", str(tmp_path))
    monkeypatch.setattr(controller, "build_suite", lambda *_: dict)
    monkeypatch.setattr(controller, "export_artifacts", lambda *_: {})


def test_controller_rejects_occupied_port_inside_unrelated_except(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controller_environment: None,
) -> None:
    with socket.socket() as competitor:
        monkeypatch.setattr(subprocess, "Popen", Mock(pid=58419))
        monkeypatch.setattr(supervisor, "_terminate_group", Mock())
        monkeypatch.setattr(
            controller,
            "wait_until_ready",
            lambda port, *_: competitor.bind((controller.HOST, port)),
        )
        unrelated = LookupError("unrelated")
        try:
            raise unrelated
        except LookupError:
            with pytest.raises(
                supervisor.SupervisorError, match="port was not released"
            ):
                controller.run_suite("patient", ["patient"])
    assert not (tmp_path / "manifest.json").exists()
    assert not hasattr(unrelated, "__notes__")


def test_controller_rejects_http_ok_without_the_owned_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controller_environment: None,
) -> None:
    process = Mock(spec=subprocess.Popen, pid=os.getpid())
    process.poll.return_value = None
    # Nothing in the fake process keeps the socket after the reservation closes.
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(supervisor, "_terminate_group", Mock())
    probe = Mock()
    probe.getresponse.return_value.status = 200
    monkeypatch.setattr(http.client, "HTTPConnection", Mock(return_value=probe))

    with pytest.raises(controller.BrowserServerError, match="does not own"):
        controller.run_suite("patient", ["patient"])

    assert probe.request.call_args.args == ("GET", "/readyz")
    assert not (tmp_path / "manifest.json").exists()


def test_controller_failed_handoff_reaps_and_proves_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controller_environment: None,
) -> None:
    primary = OSError("writer close failed after real spawn")
    popen, close = subprocess.Popen, os.close
    proof = Mock(wraps=supervisor.require_released_port)
    monkeypatch.setattr(supervisor, "require_released_port", proof)
    spawned: list[subprocess.Popen[bytes]] = []
    writer_fd: int | None = None

    def spawn(
        _argv: list[str], *, pass_fds: tuple[int, ...], **kwargs: object
    ) -> subprocess.Popen[bytes]:
        process = popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            pass_fds=pass_fds,
            **cast("dict[str, Any]", kwargs),
        )
        spawned.append(process)
        nonlocal writer_fd
        writer_fd = pass_fds[-1]
        return process

    def fail_writer(fd: int) -> None:
        nonlocal writer_fd
        close(fd)
        if fd == writer_fd:
            writer_fd = None
            raise primary

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "close", fail_writer)
    try:
        with pytest.raises(OSError, match="after real spawn") as raised:
            controller.run_suite("patient", ["patient"])
        assert raised.value is primary
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
        proof.assert_called_once()
        assert not (tmp_path / "manifest.json").exists()
    finally:
        for process in spawned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_foreign_readiness_cannot_use_a_stale_inherited_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controller_environment: None
) -> None:
    process = Mock(spec=subprocess.Popen, pid=os.getpid())
    process.poll.return_value = None
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(supervisor, "_terminate_group", Mock())
    monkeypatch.setattr(controller, "_owns_listener", lambda *_: True)
    probe = Mock()
    probe.getresponse.return_value.status = 200
    probe.getresponse.return_value.getheader.return_value = "a" * 64
    monkeypatch.setattr(http.client, "HTTPConnection", Mock(return_value=probe))
    with pytest.raises(controller.BrowserServerError, match="did not authenticate"):
        controller.run_suite("patient", ["patient"])
    assert not (tmp_path / "manifest.json").exists()


def test_controller_cancels_after_published_spawn_before_assignment(
    monkeypatch: pytest.MonkeyPatch, controller_environment: None
) -> None:
    primary = KeyboardInterrupt("cancel at caller handoff")
    master = Mock()
    proof = Mock(wraps=supervisor.require_released_port)
    monkeypatch.setattr(supervisor, "require_released_port", proof)

    def interrupted(*_args: object, owner: list[supervisor.SupervisedMaster]) -> None:
        owner.append(master)
        raise primary

    monkeypatch.setattr(controller, "start_server", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        controller.run_suite("patient", ["patient"])
    assert caught.value is primary
    master.terminate.assert_called_once()
    proof.assert_called_once()
