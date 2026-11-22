"""Runtime PHI-sentinel sinks of one browser suite run (todo 73).

The renewal runner wraps every registered suite with this module:

1. ``broker_tap`` subscribes to the suite's realtime Redis broker before the
   suite starts and keeps every published frame, the payload the SSE stream
   forwards to browsers. Suites without realtime have no SSE sink.
2. After the suite passes, while its server still runs, ``inject_probes``
   sends fresh sentinels through the live server's own surfaces: the login
   form, the UI API command search and the realtime ticket endpoint, all in
   request bodies. Their responses' redirect headers and page links are kept.
3. ``scan_suite_runtime`` then scans, for the shared ``SINTETICO-SENTINELA``
   prefix (which also catches every sentinel a suite injects itself) and the
   probe sentinels: the server and access logs, every URL the suite's
   browsers requested (``request-urls.json`` from the browser conftest) plus
   the probe responses' URLs, the broker frames, every text, JSON, array and
   bytea column of the suite database read as the superuser (audit and
   outbox rows classified separately), and the whole artifact tree.

No push sender exists, so ``push`` is reported not applicable. The shared
prefix is reserved for forbidden-sink sentinels: fixtures must not use it for
ordinary plaintext data (an organization name, a label), or the scan reports it.
Findings name the needle, so a fresh, run-unique probe value (``PROBE-...``)
is told apart from a prefix match of a suite's own sentinel. The receipt
``sentinel-runtime.json`` lists sink counts and findings by label only.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.cookiejar import CookieJar
from typing import TYPE_CHECKING, Final, Never

from ops.testing.browser_server_supervisor import (
    always_cleanup,
    start_signal_blocked_thread,
)
from ops.testing.isolation_common import IsolationError, canonical_bytes
from ops.testing.sentinel_scan import (
    SENTINEL_PREFIX,
    SentinelScan,
    header_urls,
    html_urls,
    new_sentinel,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject

RECEIPT: Final = "sentinel-runtime.json"
REQUEST_URLS: Final = "request-urls.json"
PROBE_TIMEOUT_SECONDS: Final = 15
TAP_POLL_SECONDS: Final = 0.5
CSRF_FIELD: Final = re.compile(rb'name="csrfmiddlewaretoken" value="([^"]+)"')


def _fail(message: str) -> Never:
    raise IsolationError(message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


@contextmanager
def broker_tap(redis_url: str | None) -> Iterator[list[bytes] | None]:
    """Collect every frame published on the suite broker; None without one."""
    if redis_url is None:
        yield None
        return
    from redis import Redis  # noqa: PLC0415 - only realtime suites need it
    from redis.client import PubSub  # noqa: PLC0415

    client = Redis.from_url(redis_url)
    # redis-py's sync Redis.pubsub() is unannotated; build the same object.
    pubsub = PubSub(client.connection_pool)
    pubsub.psubscribe("*")
    frames: list[bytes] = []
    stop = threading.Event()

    def listen() -> None:
        while not stop.is_set():
            message = pubsub.get_message(timeout=TAP_POLL_SECONDS)
            if (
                message is not None
                and message.get("type") == "pmessage"
                and isinstance(message.get("data"), bytes)
            ):
                frames.append(message["channel"] + b" " + message["data"])

    worker = threading.Thread(target=listen, name="sentinel-broker-tap", daemon=True)
    with (
        always_cleanup(client.close, "broker client close failed"),
        always_cleanup(pubsub.close, "broker subscription close failed"),
    ):
        start_signal_blocked_thread(worker)
        with (
            always_cleanup(worker.join, "broker tap join failed"),
            always_cleanup(stop.set, "broker tap stop failed"),
        ):
            yield frames


def inject_probes(
    base_url: str,
) -> tuple[tuple[str, ...], list[str], dict[str, int]]:
    """Send fresh sentinels through the live server.

    Returns the sentinels, every URL the responses exposed and each POST's
    HTTP status (evidence that the probe reached the view).
    """
    jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect()
    )
    sentinels: list[str] = []
    urls: list[str] = []
    statuses: dict[str, int] = {}

    def send(path: str, body: bytes | None, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(  # noqa: S310 - loopback runner URL
            base_url + path,
            data=body,
            headers=headers,
            method="POST" if body else "GET",
        )
        try:
            with opener.open(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
                content: bytes = response.read()
                urls.extend(header_urls(response.headers.get))
                status = int(response.status)
        except urllib.error.HTTPError as error:
            content = error.read()
            urls.extend(header_urls(error.headers.get))
            status = error.code
        if body is not None:
            statuses[path] = status
        urls.append(path)
        urls.extend(html_urls(content))
        return content

    form = send("/auth/login/", None, {})
    token = CSRF_FIELD.search(form)
    if token is None:
        _fail("the login page offered no CSRF token for the sentinel probe")
    csrf = token.group(1).decode()
    username, password, query, topic = (
        new_sentinel(label)
        for label in ("PROBE-USERNAME", "PROBE-PASSWORD", "PROBE-QUERY", "PROBE-TOPIC")
    )
    sentinels.extend((username, password, query, topic))
    send(
        "/auth/login/",
        urllib.parse.urlencode(
            {"csrfmiddlewaretoken": csrf, "username": username, "password": password}
        ).encode(),
        {"Content-Type": "application/x-www-form-urlencoded", "Referer": base_url},
    )
    for path, payload in (
        ("/api/ui/v1/command/search/", {"q": query}),
        ("/rt/stream", {"topics": [topic]}),
    ):
        send(
            path,
            json.dumps(payload).encode(),
            {"Content-Type": "application/json", "X-CSRFToken": csrf},
        )
    return tuple(sentinels), urls, statuses


def suite_injects_sentinels(paths: tuple[str, ...], repository: Path) -> bool:
    """Return whether the suite's own test files inject prefixed sentinels."""
    return any(
        SENTINEL_PREFIX in (repository / path).read_text(encoding="utf-8")
        for path in paths
    )


def scan_suite_runtime(  # noqa: PLR0913 - one receipt needs every sink
    *,
    suite: str,
    artifact_root: Path,
    browser_root: Path,
    base_url: str,
    frames: list[bytes] | None,
    super_dsn: str,
    self_injecting: bool,
) -> JsonObject:
    """Scan every runtime sink of the finished suite and write the receipt."""
    sentinels, probe_urls, statuses = inject_probes(base_url)
    scan = SentinelScan((SENTINEL_PREFIX, *sentinels))
    for name in ("server.log", "access.log"):
        path = artifact_root / name
        if path.is_file():
            scan.logs(path.read_text(encoding="utf-8", errors="replace").splitlines())
    recorded = browser_root / REQUEST_URLS
    if not recorded.is_file():
        _fail(f"{suite} recorded no browser request URLs")
    requested = json.loads(recorded.read_text(encoding="utf-8"))
    if not isinstance(requested, list):
        _fail(f"{suite} browser request URL record is not a list")
    scan.urls("browser", [str(url) for url in requested])
    scan.urls("probes", probe_urls)
    if frames is not None:
        for number, frame in enumerate(frames, start=1):
            scan.text("sse", f"frame {number}", frame)
        scan.scanned.setdefault("sse", 0)
    import psycopg  # noqa: PLC0415 - runner dependency, imported at scan time

    with psycopg.connect(super_dsn) as connection, connection.cursor() as cursor:
        scan.database(cursor)
    scan.files(artifact_root)
    result = scan.result()
    receipt: JsonObject = {
        **result,
        "schema": "clinic-sentinel-runtime-v1",
        "suite": suite,
        "probes": len(sentinels),
        "probe_statuses": dict(sorted(statuses.items())),
        "browser_requests": len(requested),
        "sse_frames": None if frames is None else len(frames),
        "self_injecting": self_injecting,
        "not_applicable": ["push"] if frames is not None else ["push", "sse"],
    }
    (artifact_root / RECEIPT).write_bytes(canonical_bytes(receipt))
    if not scan.clean:
        _fail(f"{suite} leaked a sentinel into a runtime sink (see {RECEIPT})")
    return receipt
