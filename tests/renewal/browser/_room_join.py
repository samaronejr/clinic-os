"""Room readiness and closed failure evidence at the document/status boundary."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from email import message_from_bytes
from hashlib import sha256
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, urlsplit

from ops.testing.isolation_common import (
    JsonObject,
    JsonValue,
    utc_now,
    write_no_replace,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect

from renewal.browser._page_wait import _answer, evaluate_js

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Page, Request

ACTIONS: Final = ("join", "status", "resume", "device_check", "audio_only")
STATES: Final = ("waiting", "active", "ended", "failed")
HISTORY: Final = """(() => {
  if (window.__clinicRoomJoin) return;
  const allowed = ["idle", "requesting", "joining", "connected", "offline",
    "reconnecting", "expired", "ended", "failed", "removed", "left"];
  const safe = value => value === null ? "absent" :
    allowed.includes(value) ? value : "other";
  const history = [];
  let omitted = 0;
  function collect(records) {
    const changes = records.filter(record => record.target.id === "room-panel");
    changes.forEach((record, index) => {
      // oldValue of the next mutation preserves intermediate states even when
      // several writes were delivered in one observer callback.
      const next = changes.slice(index + 1).find(item =>
        item.target === record.target);
      history.push({
        recorded_at_epoch_ms: performance.timeOrigin + performance.now(),
        from: safe(record.oldValue),
        to: safe(next ? next.oldValue : record.target.getAttribute("data-connection"))
      });
      if (history.length > 64) { history.shift(); omitted += 1; }
    });
  }
  const observer = new MutationObserver(collect);
  observer.observe(document, {subtree: true, attributes: true,
    attributeFilter: ["data-connection"], attributeOldValue: true});
  window.__clinicRoomJoin = () => {
    collect(observer.takeRecords());
    return {history: history.slice(), omitted: omitted};
  };
})();"""
SNAPSHOT: Final = """() => {
  const allowed = ["idle", "requesting", "joining", "connected", "offline",
    "reconnecting", "expired", "ended", "failed", "removed", "left"];
  const panels = Array.from(document.querySelectorAll("#room-panel"));
  const root = document.querySelector("[data-teleconsult]");
  const kind = root && root.getAttribute("data-teleconsult");
  return {
    ready_state: document.readyState,
    online: navigator.onLine,
    surface: ["waiting", "room", "clinician"].includes(kind) ? kind : "other",
    panel_count: panels.length,
    connections: panels.map(panel => {
      const value = panel.getAttribute("data-connection");
      return allowed.includes(value) ? value : "other";
    }),
    transitions: window.__clinicRoomJoin ? window.__clinicRoomJoin() : null
  };
}"""


def request_action(request: Request, url: str) -> str | None:
    """Read only the closed action code; never retain a body, selector or token."""
    if request.method != "POST" or request.url != url:
        return None
    content_type = request.headers.get("content-type", "")
    values: list[str] = []
    if content_type.startswith("multipart/form-data;"):
        message = message_from_bytes(
            f"Content-Type: {content_type}\r\n\r\n".encode()
            + (request.post_data_buffer or b"")
        )
        for part in message.walk():
            if part.get_param("name", header="content-disposition") != "action":
                continue
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes) and payload in (
                action.encode() for action in ACTIONS
            ):
                values.append(payload.decode("ascii"))
    elif content_type.startswith("application/x-www-form-urlencoded"):
        values = parse_qs(request.post_data or "").get("action", [])
    return values[0] if len(values) == 1 and values[0] in ACTIONS else None


def status_payload(body: bytes) -> JsonObject:
    """Keep state and closed booleans, not translated labels or arbitrary JSON."""
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"body": "not_json"}
    if not isinstance(value, dict):
        return {"body": "not_object"}
    state = value.get("state")
    result: JsonObject = {
        "state": state if isinstance(state, str) and state in STATES else "other"
    }
    for key in (
        "removed",
        "physician_joined",
        "audio_only",
        "physician_audio_only",
    ):
        flag = value.get(key)
        if isinstance(flag, bool):
            result[key] = flag
    return result


def is_status_request(request: Request, url: str) -> bool:
    return (
        request_action(request, url) == "status"
        and request.headers.get("accept") == "application/json"
    )


def capture_room_failure(page: Page, url: str, root: Path, phase: str) -> None:
    """Read native request history and closed DOM state only after failure."""
    participant = (
        "patient" if urlsplit(url).path == "/patient/teleconsult/" else "physician"
    )
    test_hash = sha256(os.environ["PYTEST_CURRENT_TEST"].encode()).hexdigest()[:12]
    destination = root / f"{participant}-room-join-{test_hash}.json"
    # Nested readiness boundaries preserve the first, closest failure snapshot.
    if destination.exists():
        return
    report: JsonObject = {
        "schema_version": 1,
        "captured_at_utc": utc_now(),
        "phase": phase,
        "participant": participant,
        "request_window_limit": 100,
        "last_status_response": None,
    }
    rows: list[JsonValue] = []
    last_status: Request | None = None
    try:
        requests: list[Request] = _answer(page, page._impl_obj.requests(), None)
        report["request_window_full"] = len(requests) == 100
        for request in requests:
            if request.method != "POST" or request.url != url:
                continue
            action = request_action(request, url) or "other"
            timing = request.timing
            response = request.existing_response
            rows.append(
                {
                    "action": action,
                    "start_epoch_ms": timing["startTime"],
                    "request_start_ms": timing["requestStart"],
                    "response_start_ms": timing["responseStart"],
                    "response_end_ms": timing["responseEnd"],
                    "status": response.status if response else None,
                    "failed": request.failure is not None,
                }
            )
            if action == "status":
                last_status = request
        if last_status is not None:
            response = last_status.existing_response
            last: JsonObject = {
                "status": response.status if response else None,
                "complete": last_status.timing["responseEnd"] >= 0,
                "failed": last_status.failure is not None,
            }
            if response is not None and last["complete"] and not last["failed"]:
                try:
                    body: bytes = _answer(page, response._impl_obj.body(), None)
                    last["payload"] = status_payload(body)
                except PlaywrightError as error:
                    last["capture_error"] = type(error).__name__
            report["last_status_response"] = last
    except PlaywrightError as error:
        report["request_capture_error"] = type(error).__name__
    report["requests"] = rows
    try:
        report["document"] = evaluate_js(page, SNAPSHOT)
    except PlaywrightError as error:
        report["document"] = {"capture_error": type(error).__name__}
    write_no_replace(
        destination,
        (json.dumps(report, sort_keys=True) + "\n").encode(),
        mode=0o600,
    )


@contextmanager
def room_join_readiness(page: Page, url: str, root: Path) -> Iterator[None]:
    """Arm before joining; apply the DOM assertion after complete status JSON."""
    phase = "observer"
    try:
        page.add_init_script(HISTORY)
        phase = "join-and-status"
        # The product paints connected only after status JSON is complete. Arm
        # before the native POST: a fast reply can finish during navigation.
        # Use the existing page deadline, not the DOM assertion's 5 s window.
        with page.expect_request_finished(
            lambda request: is_status_request(request, url)
        ) as status:
            yield
        response = status.value.existing_response
        assert response is not None
        assert response.status == 200
        phase = "connected"
        expect(page.locator("#room-panel")).to_have_attribute(
            "data-connection", "connected"
        )
    except (AssertionError, PlaywrightError) as error:
        try:
            capture_room_failure(page, url, root, phase)
        except OSError as capture_error:
            error.add_note(
                f"Join evidence write failed: {type(capture_error).__name__}"
            )
        raise
