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
    from typing import Literal

    from playwright.sync_api import Page, Request

ACTIONS: Final = ("join", "status", "resume", "device_check", "audio_only")
STATES: Final = ("waiting", "active", "ended", "failed")
MEDIA_SETTLED: Final = """(() => {
  if (window.__clinicRoomMedia) { window.__clinicRoomMedia(); return; }
  const outcomes = ["ready", "partial", "denied", "missing", "error", "insecure",
    "off", "lost"];
  let pending = false;
  const observer = new MutationObserver(records => {
    const changes = records.filter(record => record.target.id === "room-panel");
    changes.forEach((record, index) => {
      const next = changes.slice(index + 1).find(item => item.target === record.target);
      const value = next ? next.oldValue : record.target.getAttribute("data-media");
      if (value === "pending") {
        pending = true;
      } else if (pending) {
        pending = false;
        console.debug("clinic-room-media-settled:" +
          (outcomes.includes(value) ? value : "other"));
      }
    });
  });
  observer.observe(document, {subtree: true, attributes: true,
    attributeFilter: ["data-media"], attributeOldValue: true});
  window.__clinicRoomMedia = () => {
    observer.takeRecords();
    pending = document.querySelector("#room-panel")
      ?.getAttribute("data-media") === "pending";
  };
  window.__clinicRoomMedia();
})();"""
MEDIA_OUTCOMES: Final = (
    "ready",
    "partial",
    "denied",
    "missing",
    "error",
    "insecure",
    "off",
    "lost",
    "other",
)
OFFLINE_SIGNAL: Final = """() => window.addEventListener("offline", () =>
  console.debug("clinic-room-offline"), {once: true})"""
TRACK_ENDED_SIGNAL: Final = """() => {
  const stream = document.querySelector("#room-panel [data-self-video]").srcObject;
  stream.getAudioTracks()[0].addEventListener("ended", () =>
    console.debug("clinic-room-track-ended"), {once: true});
}"""
ROOM_STATE_SIGNAL: Final = """(state) => {
  const settled = () => document.querySelector("#room-panel")
    ?.getAttribute("data-connection") === state;
  const report = () => {
    if (settled()) {
      observer.disconnect();
      console.debug("clinic-room-state:" + state);
    }
  };
  const observer = new MutationObserver(report);
  observer.observe(document, {subtree: true, childList: true,
    attributes: true, attributeFilter: ["data-connection"]});
  const ready = settled();
  if (ready) report();
  return ready;
}"""
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


@contextmanager
def room_action_finished(
    page: Page, url: str, *, action: str = "status", status: int = 200
) -> Iterator[None]:
    """Subscribe before the trigger; require the exact JSON action's full reply."""
    with page.expect_request_finished(
        lambda request: (
            request_action(request, url) == action
            and request.headers.get("accept") == "application/json"
        )
    ) as finished:
        yield
    response = finished.value.existing_response
    assert response is not None
    assert response.status == status


def refresh_room_state(
    page: Page, state: Literal["expired", "ended", "failed", "removed"]
) -> None:
    """Observe terminal state without requiring another consumed status body."""
    with page.expect_console_message(
        predicate=lambda message: (
            message.type == "debug" and message.text == f"clinic-room-state:{state}"
        )
    ):
        # Expiry consumes headers only; a prior poll can already be terminal.
        script = f"({ROOM_STATE_SIGNAL})({json.dumps(state)})"
        page.add_init_script(script)
        if evaluate_js(page, script) is not True:
            page.locator("[data-refresh-status]").click()


@contextmanager
def room_media_settled(page: Page) -> Iterator[None]:
    """Observe this acquisition's pending-to-outcome transition, not stale ready."""
    page.add_init_script(MEDIA_SETTLED)
    with page.expect_console_message(
        predicate=lambda message: (
            message.type == "debug"
            and message.text
            in {f"clinic-room-media-settled:{state}" for state in MEDIA_OUTCOMES}
        )
    ):
        evaluate_js(page, MEDIA_SETTLED)
        yield


@contextmanager
def room_offline(page: Page) -> Iterator[None]:
    """Require the browser's offline event before asserting its room effects."""
    with page.expect_console_message(
        predicate=lambda message: (
            message.type == "debug" and message.text == "clinic-room-offline"
        )
    ):
        evaluate_js(page, OFFLINE_SIGNAL)
        yield


@contextmanager
def room_track_ended(page: Page) -> Iterator[None]:
    """Drain the original audio track's ended event, including loopback engines."""
    with page.expect_console_message(
        predicate=lambda message: (
            message.type == "debug" and message.text == "clinic-room-track-ended"
        )
    ):
        evaluate_js(page, TRACK_ENDED_SIGNAL)
        yield


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
        with room_action_finished(page, url):
            yield
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
