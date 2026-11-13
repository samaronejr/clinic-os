"""The runner's per-suite runtime sentinel scan fails on every planted leak."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import pytest
from ops.testing import sentinel_runtime
from ops.testing.isolation_common import IsolationError
from ops.testing.sentinel_scan import new_sentinel

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject

pytestmark = pytest.mark.django_db(transaction=True)
LOGIN_FORM = b'<form><input name="csrfmiddlewaretoken" value="synthetic-csrf"></form>'


@contextmanager
def _server(*, echo_username: bool) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(LOGIN_FORM)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            username = parse_qs(body.decode()).get("username", [""])[0]
            self.send_response(403 if self.path != "/auth/login/" else 200)
            if echo_username and username:
                self.send_header("Location", f"/next/?u={username}")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _run(tmp_path: Path, name: str) -> tuple[Path, Path]:
    artifact = tmp_path / name
    browser = artifact / "browser"
    browser.mkdir(parents=True)
    (artifact / "server.log").write_text('{"message": "http request"}\n', "utf-8")
    (artifact / "access.log").write_text("POST 200 1\n", encoding="utf-8")
    (browser / sentinel_runtime.REQUEST_URLS).write_text(
        json.dumps(["http://127.0.0.1/workspace/"]), encoding="utf-8"
    )
    return artifact, browser


def _scan(base_url: str, artifact: Path, browser: Path, dsn: str) -> JsonObject:
    return sentinel_runtime.scan_suite_runtime(
        suite="sample",
        base_url=base_url,
        artifact_root=artifact,
        browser_root=browser,
        frames=[b"rt:abc {}"],
        super_dsn=dsn,
        self_injecting=False,
    )


def test_clean_run_writes_a_receipt_with_every_sink(
    tmp_path: Path, superuser_database_url: str
) -> None:
    artifact, browser = _run(tmp_path, "clean")
    with _server(echo_username=False) as base_url:
        receipt = _scan(base_url, artifact, browser, superuser_database_url)
    assert receipt["clean"] is True
    assert receipt["probe_statuses"] == {
        "/api/ui/v1/command/search/": 403,
        "/auth/login/": 200,
        "/rt/stream": 403,
    }
    scanned = receipt["scanned"]
    assert isinstance(scanned, dict)
    assert {"logs", "urls", "sse", "database", "audit", "outbox", "files"} <= set(
        scanned
    )
    written = json.loads((artifact / sentinel_runtime.RECEIPT).read_text("utf-8"))
    assert written["not_applicable"] == ["push"]


def test_sentinel_in_a_server_log_fails_the_suite(
    tmp_path: Path, superuser_database_url: str
) -> None:
    artifact, browser = _run(tmp_path, "log")
    (artifact / "server.log").write_text(new_sentinel("SUITE") + "\n", "utf-8")
    with (
        _server(echo_username=False) as base_url,
        pytest.raises(IsolationError, match="leaked a sentinel"),
    ):
        _scan(base_url, artifact, browser, superuser_database_url)
    written = json.loads((artifact / sentinel_runtime.RECEIPT).read_text("utf-8"))
    # A suite's own sentinels are caught by the shared prefix needle.
    assert {"sink": "logs", "location": "line 1", "sentinel": "prefix"} in written[
        "findings"
    ]


def test_probe_sentinel_echoed_in_a_redirect_fails_the_suite(
    tmp_path: Path, superuser_database_url: str
) -> None:
    artifact, browser = _run(tmp_path, "echo")
    with (
        _server(echo_username=True) as base_url,
        pytest.raises(IsolationError, match="leaked a sentinel"),
    ):
        _scan(base_url, artifact, browser, superuser_database_url)
    written = json.loads((artifact / sentinel_runtime.RECEIPT).read_text("utf-8"))
    assert {
        "sink": "urls",
        "location": "probes",
        "sentinel": "PROBE-USERNAME",
    } in written["findings"]


def test_missing_browser_url_record_is_refused(
    tmp_path: Path, superuser_database_url: str
) -> None:
    artifact, browser = _run(tmp_path, "missing")
    (browser / sentinel_runtime.REQUEST_URLS).unlink()
    with (
        _server(echo_username=False) as base_url,
        pytest.raises(IsolationError, match="recorded no browser request URLs"),
    ):
        _scan(base_url, artifact, browser, superuser_database_url)


def test_suites_without_realtime_have_no_broker_tap() -> None:
    with sentinel_runtime.broker_tap(None) as frames:
        assert frames is None
