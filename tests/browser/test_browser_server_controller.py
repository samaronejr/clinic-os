from __future__ import annotations

import errno
import http.client
import socket
import subprocess
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

import pytest
from ops.testing import browser_server_controller as controller

if TYPE_CHECKING:
    from pathlib import Path


def test_gunicorn_port_stays_reserved_at_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in controller.REQUIRED_ENVIRONMENT:
        monkeypatch.setenv(name, "synthetic")
    monkeypatch.setenv("CLINIC_BROWSER_EVIDENCE_ROOT", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    (tmp_path / "cpi_flake3_ready.py").write_text(
        "def application(environ, start_response):\n"
        "    start_response('200 OK', [])\n"
        "    return [b'ready']\n",
        encoding="utf-8",
    )
    spawn = subprocess.Popen

    def contend_then_spawn(
        argv: list[str], **kwargs: object
    ) -> subprocess.Popen[bytes]:
        binding = argv[argv.index("--bind") + 1]
        if binding.startswith("fd://"):
            fd = int(binding.removeprefix("fd://"))
            with socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM) as inherited:
                port = int(inherited.getsockname()[1])
        else:
            port = int(binding.rsplit(":", 1)[1])
        # A competing binder at the exact launch seam must see an owned port.
        with (
            socket.socket() as contender,
            pytest.raises(OSError, match=rf"\[Errno {errno.EADDRINUSE}\]"),
        ):
            contender.bind((controller.HOST, port))
        return spawn(
            [*argv[:-1], "cpi_flake3_ready:application"], **cast("Any", kwargs)
        )

    def check_ready(port: int, _server_log: Path) -> None:
        # One bounded request, queued on the inherited listener until Gunicorn
        # serves it even if the worker has not booted when Popen returns.
        connection = http.client.HTTPConnection(controller.HOST, port, timeout=10)
        try:
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            assert response.status == HTTPStatus.OK
            assert response.read() == b"ready"
        finally:
            connection.close()

    monkeypatch.setattr(subprocess, "Popen", contend_then_spawn)
    monkeypatch.setattr(controller, "wait_until_ready", check_ready)
    monkeypatch.setattr(controller, "build_suite", lambda *_: dict)
    monkeypatch.setattr(controller, "export_artifacts", lambda *_: {})

    assert controller.run_suite("patient", ["patient"]) == 0
