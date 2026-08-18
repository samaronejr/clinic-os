"""Boot one supervised product server and dispatch a browser suite against it."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Callable

from ops.testing.browser_artifact_publisher import (
    frame_suite_artifacts,
    manifest_digest,
    publication_acknowledgement,
    require_acknowledged,
)
from ops.testing.browser_runner_contract import selected_suites
from ops.testing.browser_suites.availability import build_availability_suite
from ops.testing.browser_suites.patient import build_patient_suite
from ops.testing.browser_totp_code import TotpContext, confirmed_code

AVAILABLE_SUITES: Final = ["availability", "patient"]
PHYSICIAN_ENVIRONMENT: Final = (
    "CLINIC_BROWSER_ORG_ID",
    "CLINIC_BROWSER_PHYSICIAN_ID",
    "CLINIC_BROWSER_PHYSICIAN_PASSWORD",
    "CLINIC_BROWSER_PHYSICIAN_USERNAME",
)
READY_TIMEOUT_SECONDS: Final = 60
READY_POLL_SECONDS: Final = 0.25
TERMINATION_GRACE_SECONDS: Final = 10
HOST: Final = "127.0.0.1"
PROTECTED_DATABASE_PORT: Final = 5432
PORT_ATTEMPTS: Final = 32
REQUIRED_ENVIRONMENT: Final = (
    "CLINIC_BROWSER_BASE_URL",
    "CLINIC_BROWSER_CLINIC_ID",
    "CLINIC_BROWSER_EVIDENCE_ROOT",
    "CLINIC_BROWSER_PASSWORD",
    "CLINIC_BROWSER_USERNAME",
)


class BrowserServerError(RuntimeError):
    """Reject a malformed or unhealthy browser-server invocation."""

    def __init__(self, reason: str) -> None:
        """Expose one stable non-identifying supervision failure message."""
        super().__init__(f"browser server rejected: {reason}")


def _fail(reason: str) -> Never:
    raise BrowserServerError(reason)


def reserve_port() -> int:
    """Return one free loopback TCP port that is never the protected 5432."""
    for _attempt in range(PORT_ATTEMPTS):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, 0))
            port = int(probe.getsockname()[1])
        if port != PROTECTED_DATABASE_PORT:
            return port
    _fail("no free loopback port")


def wait_until_ready(port: int, server_log: Path) -> None:
    """Poll the readiness endpoint until the supervised app serves traffic."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last = "no attempt"
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(HOST, port, timeout=2)
        try:
            connection.request("GET", "/readyz")
            status = connection.getresponse().status
            if status == HTTPStatus.OK:
                return
            last = f"status {status}"
        except (OSError, http.client.HTTPException) as error:
            last = type(error).__name__
        finally:
            connection.close()
        time.sleep(READY_POLL_SECONDS)
    tail = ""
    if server_log.exists():
        tail = server_log.read_text(encoding="utf-8", errors="replace")[-2000:]
    _fail(f"readiness never reached ({last}); server log tail: {tail}")


def start_server(
    port: int,
    environment: dict[str, str],
    server_log: Path,
) -> subprocess.Popen[bytes]:
    """Fork and exec one supervised Gunicorn master in its own process group."""
    interpreter = Path(sys.executable)
    if not interpreter.is_absolute() or not os.access(interpreter, os.X_OK):
        _fail("supervised interpreter is not an executable absolute path")
    with server_log.open("wb") as stream:
        return subprocess.Popen(  # noqa: S603 - resolved interpreter, closed argv.
            [
                str(interpreter),
                "-m",
                "gunicorn",
                "--bind",
                f"{HOST}:{port}",
                "--workers=2",
                "--preload",
                "--access-logfile",
                environment["CLINIC_BROWSER_ACCESS_LOG"],
                "--error-logfile",
                "-",
                "config.wsgi:application",
            ],
            env=environment,
            start_new_session=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )


def stop_server(process: subprocess.Popen[bytes]) -> None:
    """Terminate the supervised process group and reap every descendant."""
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=TERMINATION_GRACE_SECONDS)


def publish(evidence_root: Path, artifacts: dict[str, bytes]) -> dict[str, str]:
    """Write the bounded allowlisted artifact set under the evidence root."""
    published: dict[str, str] = {}
    for relative, content in sorted(artifacts.items()):
        destination = evidence_root / relative
        if not destination.resolve().is_relative_to(evidence_root.resolve()):
            _fail("artifact escaped the evidence root")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o600)
        published[relative] = hashlib.sha256(content).hexdigest()
    return published


def _base_config(base_url: str) -> dict[str, str]:
    return {
        "base_url": base_url,
        "clinic_id": os.environ["CLINIC_BROWSER_CLINIC_ID"],
        "password": os.environ["CLINIC_BROWSER_PASSWORD"],
        "username": os.environ["CLINIC_BROWSER_USERNAME"],
    }


def build_suite(suite_id: str, base_url: str) -> Callable[[], dict[str, bytes]]:
    """Return the allowlisted zero-argument entrypoint for one suite id."""
    if suite_id == "patient":
        return build_patient_suite(_base_config(base_url))
    if suite_id != "availability":
        _fail(f"{suite_id} is not an implemented browser suite")
    for name in PHYSICIAN_ENVIRONMENT:
        if not os.environ.get(name):
            _fail(f"missing {name}")
    context = TotpContext(
        dsn=os.environ["APP_DATABASE_URL"],
        tenant_id=os.environ["CLINIC_BROWSER_ORG_ID"],
        user_id=os.environ["CLINIC_BROWSER_PHYSICIAN_ID"],
    )
    config = _base_config(base_url)
    config["physician_username"] = os.environ["CLINIC_BROWSER_PHYSICIAN_USERNAME"]
    config["physician_password"] = os.environ["CLINIC_BROWSER_PHYSICIAN_PASSWORD"]
    return build_availability_suite(config, lambda: confirmed_code(context))


def export_artifacts(
    evidence_root: Path,
    suite_id: str,
    artifacts: dict[str, bytes],
) -> dict[str, str]:
    """Frame, publish, and require acknowledgement of one bounded artifact set."""
    frames = frame_suite_artifacts(suite_id, artifacts)
    published = publish(evidence_root, artifacts)
    digest = manifest_digest(frames)
    require_acknowledged(
        publication_acknowledgement(suite_id, digest), suite_id, digest
    )
    return published


def run_suite(suite_id: str, required: list[str]) -> int:
    """Supervise one server, run the selected suite, and publish evidence."""
    selected = selected_suites(AVAILABLE_SUITES, required)
    if suite_id not in selected:
        _fail(f"{suite_id} was not required by this session")
    for name in REQUIRED_ENVIRONMENT:
        if not os.environ.get(name):
            _fail(f"missing {name}")
    evidence_root = Path(os.environ["CLINIC_BROWSER_EVIDENCE_ROOT"])
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    port = reserve_port()
    environment = dict(os.environ)
    environment["CLINIC_BROWSER_ACCESS_LOG"] = str(evidence_root / "access.log")
    server_log = evidence_root / "server.log"
    server = start_server(port, environment, server_log)
    try:
        wait_until_ready(port, server_log)
        artifacts = build_suite(suite_id, f"http://{HOST}:{port}")()
    finally:
        stop_server(server)
    manifest = {
        "artifacts": export_artifacts(evidence_root, suite_id, artifacts),
        "schema_version": 1,
        "suite_ids": [suite_id],
    }
    manifest_path = evidence_root / "manifest.json"
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    )
    manifest_path.chmod(0o600)
    sys.stdout.write(f"{manifest_path}\n")
    return 0


def parse_suite_arguments(arguments: list[str]) -> tuple[str, list[str]]:
    """Return the dispatched suite and its sorted unique required-suite set."""
    if not arguments or arguments[0] != "suite":
        _fail("invalid operation")
    rest = arguments[1:]
    suite_id = ""
    if rest and not rest[0].startswith("--"):
        suite_id = rest[0]
        rest = rest[1:]
    if len(rest) % 2:
        _fail("invalid suite grammar")
    required: list[str] = []
    for index in range(0, len(rest), 2):
        if rest[index] != "--require-suite":
            _fail("invalid suite grammar")
        required.append(rest[index + 1])
    if not suite_id:
        suite_id = sorted(set(required))[0] if required else "patient"
    return suite_id, sorted(set(required)) or [suite_id]


def main() -> int:
    """Dispatch the single supported suite operation after shell validation."""
    suite_id, required = parse_suite_arguments(sys.argv[1:])
    return run_suite(suite_id, required)


if __name__ == "__main__":
    sys.exit(main())
