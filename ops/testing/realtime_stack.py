"""Task-owned Redis/SSE processes and a same-origin browser-test reverse proxy."""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final
from urllib.parse import urlsplit
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

REDIS_IMAGE: Final = (
    "redis:7-alpine@sha256:"
    "858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
)
READY_SECONDS: Final = 30


class RealtimeHarnessError(RuntimeError):
    """Fail loudly without reflecting configuration, URLs or credentials."""


def _stop(process: subprocess.Popen[str], repository: Path) -> None:
    if process.poll() is not None:
        return
    if Path(f"/proc/{process.pid}/cwd").resolve() != repository.resolve():
        message = "realtime harness process ownership changed"
        raise RealtimeHarnessError(message)
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _ready(
    process: subprocess.Popen[str], marker: str, log: IO[str]
) -> threading.Thread:
    ready = threading.Event()

    def read() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            log.write(line)
            log.flush()
            if marker in line:
                ready.set()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    if not ready.wait(READY_SECONDS):
        message = "realtime harness did not emit readiness"
        raise RealtimeHarnessError(message)
    return thread


@contextmanager
def redis_server(repository: Path, root: Path) -> Iterator[str]:
    """Wait for Redis's exact readiness event, never a fixed startup sleep."""
    name = "clinic-realtime-test-" + uuid4().hex
    argv = [
        "/usr/bin/docker",
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "clinic.realtime.test=true",
        "-p",
        "127.0.0.1::6379",
        REDIS_IMAGE,
        "redis-server",
        "--save",
        "",
        "--appendonly",
        "no",
    ]
    subprocess.run(argv, check=True, capture_output=True, cwd=repository)  # noqa: S603 - owned container, fixed binary.
    observer = None
    try:
        with (root / "redis.log").open("w") as log:
            observer = subprocess.Popen(  # noqa: S603 - fixed docker logs for owned container.
                ["/usr/bin/docker", "logs", "--follow", name],
                cwd=repository,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            thread = _ready(observer, "Ready to accept connections", log)
            result = subprocess.run(  # noqa: S603 - inspect the exact owned container.
                [
                    "/usr/bin/docker",
                    "inspect",
                    "--format",
                    '{{(index (index .NetworkSettings.Ports "6379/tcp") 0).HostPort}}',
                    name,
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=repository,
            )
            # The prefork supervisor requires a single-threaded spawn/handoff.
            # Readiness is a one-shot observation, not a session-long log pump.
            _stop(observer, repository)
            thread.join(timeout=10)
            if thread.is_alive():
                message = "realtime readiness observer did not stop"
                raise RealtimeHarnessError(message)
            yield f"redis://127.0.0.1:{int(result.stdout.strip())}/0"
    finally:
        if observer is not None:
            _stop(observer, repository)
            if observer.stdout is not None:
                observer.stdout.close()
        subprocess.run(  # noqa: S603 - fixed Docker argv; exact owned container.
            # Redis declares VOLUME /data even with persistence disabled.
            # Remove that owned anonymous volume, not just its container.
            ["/usr/bin/docker", "rm", "-f", "-v", name],
            check=True,
            capture_output=True,
            cwd=repository,
        )


class RealtimeStack:
    """Expose only the new process PID and same-origin port to synthetic QA."""

    def __init__(
        self, repository: Path, environment: dict[str, str], root: Path, web_port: int
    ) -> None:
        """Retain input privately; no credentials enter reports."""
        self.repository = repository
        self.environment = environment
        self.root = root
        self.web_port = web_port
        self.process: subprocess.Popen[str] | None = None
        self.port = 0

    def stop_realtime(self) -> None:
        """Exercise genuine process failure; the ordinary web process keeps serving."""
        if self.process is not None:
            _stop(self.process, self.repository)

    @contextmanager
    def run(self) -> Iterator[RealtimeStack]:
        """Own sockets, thread, process and logs through all exception paths."""
        with socket.socket() as listener, (self.root / "realtime.log").open("w") as log:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            rt_port = int(listener.getsockname()[1])
            self.process = subprocess.Popen(  # noqa: S603 - fixed interpreter/uvicorn entrypoint.
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "config.asgi_realtime:application",
                    "--fd",
                    str(listener.fileno()),
                    "--no-access-log",
                    "--no-proxy-headers",
                    "--lifespan=off",
                    "--timeout-graceful-shutdown=1",
                ],
                cwd=self.repository,
                env={**self.environment, "CLINIC_REALTIME_MODE": "renewal"},
                pass_fds=(listener.fileno(),),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            proxy = None
            try:
                thread = _ready(self.process, "Uvicorn running on", log)
                listener.close()
                proxy = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    _proxy_handler(self.web_port, rt_port, self.stop_realtime),
                )
                self.port = int(proxy.server_address[1])
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    yield self
                finally:
                    proxy.shutdown()
                    proxy_thread.join(timeout=10)
                    self.stop_realtime()
                    thread.join(timeout=10)
            finally:
                self.stop_realtime()
                if self.process.stdout is not None:
                    self.process.stdout.close()
                if proxy is not None:
                    proxy.server_close()


@contextmanager
def realtime_broker(
    repository: Path,
    environment: dict[str, str],
    root: Path,
    *,
    enabled: bool,
) -> Iterator[None]:
    """Prepare Redis before the WSGI spawn without retaining observer threads."""
    if not enabled:
        yield
        return
    with redis_server(repository, root) as broker:
        environment.update(REALTIME_ENABLED="true", REALTIME_REDIS_URL=broker)
        yield


@contextmanager
def browser_endpoint(
    repository: Path,
    environment: dict[str, str],
    root: Path,
    web_port: int,
    *,
    enabled: bool,
) -> Iterator[int]:
    """Keep the ordinary browser route unchanged outside the realtime suite."""
    if not enabled:
        yield web_port
        return
    # Enter only after the WSGI master handoff. The ASGI logger and same-origin
    # proxy can then own threads without invalidating prefork signal masking.
    with RealtimeStack(repository, environment, root, web_port).run() as stack:
        yield stack.port


def _proxy_handler(
    web_port: int, realtime_port: int, stop_realtime: Callable[[], None]
) -> type[BaseHTTPRequestHandler]:
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args: object, **kwargs: object) -> None:
            # Tickets/cookies are deliberately never logged by this test proxy.
            del args, kwargs

        def do_GET(self) -> None:
            self.forward()

        def do_POST(self) -> None:
            if self.path == "/_realtime-test/stop":
                stop_realtime()
                self.send_response(204)
                self.end_headers()
                return
            self.forward()

        def forward(self) -> None:
            stream = self.command == "GET" and urlsplit(self.path).path == "/rt/stream"
            upstream = http.client.HTTPConnection(
                "127.0.0.1", realtime_port if stream else web_port, timeout=20
            )
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                upstream.request(self.command, self.path, body, dict(self.headers))
                response = upstream.getresponse()
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in {"connection", "transfer-encoding"}:
                        self.send_header(name, value)
                self.end_headers()
                while data := response.read1(65536):
                    self.wfile.write(data)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                # Client disconnects and deliberate realtime process death are
                # transport termination, never a successful synthetic response.
                self.close_connection = True
            finally:
                upstream.close()

    return Proxy
