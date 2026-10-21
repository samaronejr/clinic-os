"""Exercise an incomplete chunk body against the real inbound worker deadline."""

from __future__ import annotations

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from renewal.browser.test_scribe_capture import (
    capture_page,
    fixture_command,
    open_capture,
)

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("capture_page",)


def _trickle(connection: socket.socket, stopped: Event) -> int:
    """Time is the behavior: continued body progress must not renew the deadline."""
    sent = 0
    while not stopped.wait(0.25):
        try:
            connection.sendall(b" ")
        except (BrokenPipeError, ConnectionResetError):
            break
        sent += 1
    return sent


def test_slow_chunk_body_is_terminated_without_ingest_mutation(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    # Given: authenticated capture authority and a complete, valid header frame.
    page = capture_page
    open_capture(page, renewal_base_url, availability_staff, "2036-04-30")
    panel = page.locator("[data-scribe-capture]")
    form = panel.locator("[data-capture-form]")
    token = form.locator('[name="encounter_token"]').input_value()
    csrf = form.locator('[name="csrfmiddlewaretoken"]').input_value()
    control = panel.get_attribute("data-control-url")
    chunk = panel.get_attribute("data-chunk-url")
    assert control is not None
    assert chunk is not None
    authorized = page.request.post(
        urljoin(renewal_base_url, control),
        data={
            "action": "authorize",
            "encounter_token": token,
            "command_id": str(uuid4()),
        },
        headers={"X-CSRFToken": csrf, "Referer": page.url},
    )
    assert authorized.status == 200
    session = authorized.json()["session_id"]
    terms = {
        "SCRIBE_FIXTURE_ACTION": "inspect",
        "SCRIBE_FIXTURE_SESSION": session,
    }
    before = fixture_command(availability_staff, terms)
    assert json.loads(before)["chunks"] == 0
    target = urlsplit(renewal_base_url)
    assert target.hostname in {"127.0.0.1", "localhost"}
    assert target.port is not None
    pairs = []
    for cookie in page.context.cookies(renewal_base_url):
        name = cookie.get("name")
        value = cookie.get("value")
        assert name is not None
        assert value is not None
        pairs.append(name + "=" + value)
    cookies = "; ".join(pairs)
    headers = (
        f"POST {chunk} HTTP/1.1\r\n"
        f"Host: {target.netloc}\r\n"
        f"Cookie: {cookies}\r\n"
        f"X-CSRFToken: {csrf}\r\n"
        f"X-Encounter-Token: {token}\r\n"
        f"Referer: {page.url}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 350000\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    stopped = Event()

    # When: whitespace keeps arriving, but the promised chunk body never ends.
    with (
        socket.create_connection((target.hostname, target.port), timeout=40) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        started = monotonic()
        client.sendall(headers + b"{")
        sending = pool.submit(_trickle, client, stopped)
        try:
            with HTTPResponse(client) as response:
                response.begin()
                status = response.status
                response.read(4096)
            elapsed = monotonic() - started
        finally:
            stopped.set()
            trickled = sending.result(timeout=5)

    # Then: the sync worker aborts the body, not a client fetch timeout.
    assert status == 500
    assert 25 <= elapsed <= 40
    assert trickled >= 10
    assert fixture_command(availability_staff, terms) == before
    health = page.request.get(urljoin(renewal_base_url, "/healthz"))
    assert health.status == 200
    (renewal_artifact_root / "slow-body.json").write_text(
        json.dumps(
            {
                "level": "L1",
                "surface": "supervised sync Gunicorn chunk POST",
                "worker_deadline_seconds": 30,
                "client_deadline_seconds": 40,
                "elapsed_seconds": elapsed,
                "body_bytes_sent": trickled + 1,
                "declared_body_bytes": 350000,
                "response_status": status,
                "capture_metadata_unchanged": True,
                "healthy_worker_after_probe": True,
            },
            sort_keys=True,
        )
        + "\n"
    )
