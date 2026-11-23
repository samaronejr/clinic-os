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
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit
from uuid import uuid4

from ops.testing.browser_server_supervisor import (
    blocked_launch_signals,
    child_argv,
    owned_child,
    start_signal_blocked_thread,
)
from ops.testing.diagnostic_redaction import (
    Failures,
    always_cleanup,
    diagnostics,
    error_details,
    preserving_context,
)

if TYPE_CHECKING:
    import signal
    from collections.abc import Callable, Iterator
    from typing import TextIO

    from ops.testing.diagnostic_redaction import DiagnosticSink

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


def _dispose_observer(process: subprocess.Popen[str], repository: Path) -> None:
    stream = process.stdout
    if stream is not None and not stream.closed:
        with always_cleanup(stream.close, "realtime stdout close failed"):
            _stop(process, repository)


@contextmanager
def _ready(
    process: subprocess.Popen[str], marker: str, log: TextIO, repository: Path
) -> Iterator[None]:
    ready = threading.Event()
    sink = diagnostics()
    failures = Failures()
    stream = process.stdout
    if stream is None:
        message = "realtime harness has no readiness stream"
        raise RealtimeHarnessError(message)

    def read() -> None:
        try:
            for line in stream:
                sink.line(log, line)
                log.flush()
                if marker in line:
                    ready.set()
        except Exception as error:  # noqa: BLE001 - pass observer failures to its owner.
            failures.record("realtime log observation failed", error)
            ready.set()

    thread = threading.Thread(target=read, daemon=True)

    def finish_log() -> None:
        if thread.ident is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                message = "realtime log observer did not stop"
                raise RealtimeHarnessError(message)
        failures.raise_if_failed()

    with (
        always_cleanup(stream.close, "realtime stdout close failed"),
        always_cleanup(finish_log, "realtime log thread join failed"),
        always_cleanup(
            lambda: _stop(process, repository), "realtime observer stop failed"
        ),
    ):
        start_signal_blocked_thread(thread)
        if not ready.wait(READY_SECONDS):
            message = "realtime harness did not emit readiness"
            raise RealtimeHarnessError(message)
        failures.raise_if_failed()
        yield


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

    def remove() -> None:
        # Redis declares VOLUME /data even with persistence disabled.
        subprocess.run(  # noqa: S603 - exact owned container.
            ["/usr/bin/docker", "rm", "-f", "-v", name],
            check=True,
            capture_output=True,
            cwd=repository,
        )

    def observe(mask: set[int | signal.Signals]) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 - fixed docker logs for owned container.
            child_argv(["/usr/bin/docker", "logs", "--follow", name], mask),
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    with always_cleanup(remove, "realtime Redis removal failed"):
        with blocked_launch_signals():
            subprocess.run(argv, check=True, capture_output=True, cwd=repository)  # noqa: S603 - owned container, fixed binary.
        with (
            diagnostics().live_log(root / "redis.log") as log,
            owned_child(
                observe, lambda process: _dispose_observer(process, repository)
            ) as observer,
            _ready(observer, "Ready to accept connections", log, repository),
        ):
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
        # WSGI preloads only after the temporary log observer has exited.
        yield f"redis://127.0.0.1:{int(result.stdout.strip())}/0"


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
        diagnostics(self.environment)
        with (
            preserving_context(
                socket.socket(), "realtime listener close failed"
            ) as listener,
            diagnostics().live_log(self.root / "realtime.log") as log,
        ):
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            rt_port = int(listener.getsockname()[1])

            def spawn(mask: set[int | signal.Signals]) -> subprocess.Popen[str]:
                self.process = subprocess.Popen(  # noqa: S603 - fixed interpreter/uvicorn entrypoint.
                    child_argv(
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
                        mask,
                    ),
                    cwd=self.repository,
                    env={**self.environment, "CLINIC_REALTIME_MODE": "renewal"},
                    pass_fds=(listener.fileno(),),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return self.process

            with (
                owned_child(
                    spawn, lambda child: _dispose_observer(child, self.repository)
                ) as process,
                _ready(process, "Uvicorn running on", log, self.repository),
                self._proxy(rt_port),
            ):
                listener.close()
                yield self

    @contextmanager
    def _proxy(self, rt_port: int) -> Iterator[None]:
        proxy = _DiagnosticProxy(
            ("127.0.0.1", 0),
            _proxy_handler(self.web_port, rt_port, self.stop_realtime),
        )
        proxy.diagnostics = diagnostics()
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)

        def join() -> None:
            if thread.ident is not None:
                thread.join(timeout=10)
                if thread.is_alive():
                    message = "realtime proxy did not stop"
                    raise RealtimeHarnessError(message)

        with (
            always_cleanup(proxy.server_close, "realtime proxy close failed"),
            always_cleanup(join, "realtime proxy join failed"),
            always_cleanup(
                lambda: proxy.shutdown() if thread.ident is not None else None,
                "realtime proxy shutdown failed",
            ),
        ):
            self.port = int(proxy.server_address[1])
            start_signal_blocked_thread(thread)
            yield


class _DiagnosticProxy(ThreadingHTTPServer):
    """Never let the standard request exception handler print a raw traceback."""

    diagnostics: DiagnosticSink

    def handle_error(self, request: object, client_address: object) -> None:
        """Render only redacted request-handler failures."""
        del request, client_address
        error = sys.exception()
        if error is not None:
            self.diagnostics.screen(
                f"realtime proxy: {error_details(error)}\n", error=True
            )


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
