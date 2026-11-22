"""Boot one supervised product server and dispatch a browser suite against it."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import socket
import sys
import time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterator

    from ops.testing.browser_server_supervisor import SupervisedMaster

from ops.testing.browser_artifact_publisher import (
    frame_suite_artifacts,
    manifest_digest,
    publication_acknowledgement,
    require_acknowledged,
)
from ops.testing.browser_runner_contract import selected_suites
from ops.testing.browser_server_supervisor import (
    always_cleanup,
    preserve_cleanup_error,
    preserving_context,
    start_master,
    teardown_master,
)
from ops.testing.browser_suites.availability import build_availability_suite
from ops.testing.browser_suites.patient import build_patient_suite
from ops.testing.browser_suites.scheduling import build_scheduling_suite
from ops.testing.browser_totp_code import TotpContext, confirmed_code

AVAILABLE_SUITES: Final = ["availability", "patient", "scheduling"]
PHYSICIAN_SUITES: Final = frozenset({"availability", "scheduling"})
PHYSICIAN_ENVIRONMENT: Final = (
    "CLINIC_BROWSER_FIXTURE_DATABASE_URL",
    "CLINIC_BROWSER_ORG_ID",
    "CLINIC_BROWSER_PHYSICIAN_ID",
    "CLINIC_BROWSER_PHYSICIAN_PASSWORD",
    "CLINIC_BROWSER_PHYSICIAN_USERNAME",
)
# The TOTP seed is resolver-held (identity 0050), so only a fixture login that
# holds SET on clinic_resolver can derive a code; Gunicorn never receives it.
FIXTURE_DATABASE_VARIABLE: Final = "CLINIC_BROWSER_FIXTURE_DATABASE_URL"
READY_TIMEOUT_SECONDS: Final = 60
READY_POLL_SECONDS: Final = 0.25
HOST: Final = "127.0.0.1"
PROTECTED_DATABASE_PORT: Final = 5432
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


@contextlib.contextmanager
def reserve_listener() -> Iterator[socket.socket]:
    """Own an ephemeral loopback listener until Gunicorn inherits it."""
    with preserving_context(
        socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        "reserved listener close failed",
    ) as listener:
        listener.bind((HOST, 0))
        if listener.getsockname()[1] == PROTECTED_DATABASE_PORT:
            _fail("ephemeral listener selected the protected database port")
        listener.listen()
        yield listener


def _readiness_failure(reason: str) -> Never:
    _fail(f"readiness never reached ({reason})")


@contextlib.contextmanager
def with_server_log(server_log: Path) -> Iterator[None]:
    """Keep the server log tail with the primary startup or teardown failure."""
    try:
        yield
    except BaseException as error:
        with (
            preserve_cleanup_error(error, "could not read server log tail"),
            contextlib.suppress(FileNotFoundError),
            server_log.open("rb") as stream,
        ):
            stream.seek(max(0, stream.seek(0, io.SEEK_END) - 2000))
            tail = stream.read(2000).decode("utf-8", errors="replace")
            BaseException.add_note(error, f"server log tail: {tail}")
        raise


def _require_running(server: subprocess.Popen[bytes]) -> None:
    status = server.poll()
    if status is not None:
        _readiness_failure(f"server exited with status {status}")


def wait_until_ready(
    port: int,
    server: subprocess.Popen[bytes],
    listener_identity: Path,
) -> None:
    """Poll the readiness endpoint until the supervised app serves traffic."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last = "no attempt"
    while time.monotonic() < deadline:
        _require_running(server)
        connection = http.client.HTTPConnection(HOST, port, timeout=2)
        with always_cleanup(connection.close, "readiness connection close failed"):
            try:
                connection.request("GET", "/readyz")
                status = connection.getresponse().status
            except (OSError, http.client.HTTPException) as error:
                last = type(error).__name__
            else:
                if status == HTTPStatus.OK:
                    _require_running(server)
                    if not _owns_listener(server.pid, listener_identity):
                        _readiness_failure("server does not own the reserved listener")
                    return
                last = f"status {status}"
        time.sleep(READY_POLL_SECONDS)
    _require_running(server)
    _readiness_failure(last)


def listener_identity(listener: socket.socket) -> Path:
    """Capture the reserved socket's kernel identity before the parent closes it."""
    return Path(f"/proc/self/fd/{listener.fileno()}").readlink()


def _owns_listener(pid: int, identity: Path) -> bool:
    try:
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            with contextlib.suppress(FileNotFoundError):
                if descriptor.readlink() == identity:
                    return True
    except FileNotFoundError:
        return False
    return False


def start_server(
    listener: socket.socket,
    environment: dict[str, str],
    server_log: Path,
    *,
    owner: list[SupervisedMaster],
) -> SupervisedMaster:
    """Fork and exec one supervised Gunicorn master in its own process group."""
    interpreter = Path(sys.executable)
    if not interpreter.is_absolute() or not os.access(interpreter, os.X_OK):
        _fail("supervised interpreter is not an executable absolute path")
    listener_fd = listener.fileno()
    return start_master(
        [
            str(interpreter),
            "-m",
            "gunicorn",
            "--bind",
            f"fd://{listener_fd}",
            "--workers=2",
            "--preload",
            "--access-logfile",
            environment["CLINIC_BROWSER_ACCESS_LOG"],
            # ADR-014: method + status + duration only; no request line.
            "--access-logformat",
            "%(m)s %(s)s %(D)s",
            "--error-logfile",
            "-",
            "config.wsgi:application",
        ],
        environment,
        server_log,
        owner=owner,
        pass_fds=(listener,),
    )


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
    if suite_id not in PHYSICIAN_SUITES:
        _fail(f"{suite_id} is not an implemented browser suite")
    for name in PHYSICIAN_ENVIRONMENT:
        if not os.environ.get(name):
            _fail(f"missing {name}")
    context = TotpContext(
        dsn=os.environ[FIXTURE_DATABASE_VARIABLE],
        tenant_id=os.environ["CLINIC_BROWSER_ORG_ID"],
        user_id=os.environ["CLINIC_BROWSER_PHYSICIAN_ID"],
    )
    config = _base_config(base_url)
    config["physician_username"] = os.environ["CLINIC_BROWSER_PHYSICIAN_USERNAME"]
    config["physician_password"] = os.environ["CLINIC_BROWSER_PHYSICIAN_PASSWORD"]
    if suite_id == "scheduling":
        return build_scheduling_suite(config, lambda: confirmed_code(context))
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
    environment = dict(os.environ)
    environment.pop(FIXTURE_DATABASE_VARIABLE, None)
    environment["CLINIC_BROWSER_ACCESS_LOG"] = str(evidence_root / "access.log")
    server_log = evidence_root / "server.log"
    spawned: list[SupervisedMaster] = []
    server: SupervisedMaster | None = None

    def cleanup() -> None:
        if server is not None:
            teardown_master(server, port)

    with (
        with_server_log(server_log),
        always_cleanup(cleanup, "browser server teardown failed"),
    ):
        with reserve_listener() as listener:
            port = int(listener.getsockname()[1])
            identity = listener_identity(listener)
            server = start_server(listener, environment, server_log, owner=spawned)
        wait_until_ready(port, server.process, identity)
        artifacts = build_suite(suite_id, f"http://{HOST}:{port}")()
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
