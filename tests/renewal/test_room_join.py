"""DB-free checks for the room join's event boundary and closed failure evidence."""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING, Final
from unittest.mock import MagicMock, PropertyMock, create_autospec

import pytest
from playwright.sync_api import ConsoleMessage, Page, Request, Response
from playwright.sync_api import Error as PlaywrightError

from renewal.browser import _room_join as join
from renewal.browser import test_clinician_video as clinician
from renewal.browser import test_patient_video as patient_video

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

    from playwright.sync_api import Locator

URL: Final = "https://clinic.invalid/patient/teleconsult/"
STAFF_URL: Final = "https://clinic.invalid/teleconsult/clinics/synthetic/"
MULTIPART: Final = (
    b'--boundary\r\nContent-Disposition: form-data; name="action"\r\n\r\n'
    b"status\r\n--boundary--\r\n"
)
TIMING: Final = {
    "startTime": 1000.0,
    "domainLookupStart": -1.0,
    "domainLookupEnd": -1.0,
    "connectStart": -1.0,
    "secureConnectionStart": -1.0,
    "connectEnd": -1.0,
    "requestStart": 1.0,
    "responseStart": 2.0,
    "responseEnd": -1.0,
}


def _request(
    body: bytes = MULTIPART,
    *,
    url: str = URL,
    method: str = "POST",
    content_type: str = "multipart/form-data; boundary=boundary",
) -> Request:
    request: Request = create_autospec(
        Request,
        instance=True,
        url=url,
        method=method,
        headers={"content-type": content_type, "accept": "application/json"},
        post_data_buffer=body,
        post_data=body.decode(),
        timing=dict(TIMING),
        existing_response=None,
        failure=None,
    )
    return request


@pytest.mark.parametrize(
    ("method", "url", "body", "content_type", "action"),
    [
        ("POST", URL, MULTIPART, "multipart/form-data; boundary=boundary", "status"),
        ("POST", URL, b"action=join", "application/x-www-form-urlencoded", "join"),
        ("POST", URL, b"action=resume", "application/x-www-form-urlencoded", "resume"),
        ("GET", URL, MULTIPART, "multipart/form-data; boundary=boundary", None),
        (
            "POST",
            URL + "other",
            MULTIPART,
            "multipart/form-data; boundary=boundary",
            None,
        ),
        ("POST", URL, b'{"action":"status"}', "application/json", None),
        ("POST", URL, b"action=private", "application/x-www-form-urlencoded", None),
        (
            "POST",
            URL,
            b"action=status&action=join",
            "application/x-www-form-urlencoded",
            None,
        ),
    ],
)
def test_action_matches_the_exact_endpoint_and_form(
    method: str, url: str, body: bytes, content_type: str, action: str | None
) -> None:
    # Given: a request including arbitrary JSON POSTs and duplicate form actions.
    request = _request(body, method=method, url=url, content_type=content_type)
    # When: the join boundary identifies the operation.
    actual = join.request_action(request, URL)
    # Then: only a single known native/form-data action is returned.
    assert actual == action


def test_status_wait_rejects_unrelated_patient_actions() -> None:
    # Given: the same patient endpoint also handles device checks and resume.
    other = _request(
        b"action=device_check", content_type="application/x-www-form-urlencoded"
    )
    # When / Then: an unrelated successful POST cannot satisfy status readiness.
    assert not join.is_status_request(other, URL)
    assert join.is_status_request(_request(), URL)


@pytest.mark.parametrize("completed_during_click", [False, True])
@pytest.mark.parametrize("url", [URL, STAFF_URL])
def test_join_subscribes_before_navigation_and_asserts_after_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    *,
    completed_during_click: bool,
) -> None:
    # Given: status can finish either during the click or after navigation returns.
    page = create_autospec(Page, instance=True)
    request = _request(url=url)
    response: Response = create_autospec(Response, instance=True, status=200)
    monkeypatch.setattr(request, "existing_response", response)
    events: list[str] = []
    completed = False
    signal: MagicMock = page.expect_request_finished.return_value
    signal.value = request

    def arm() -> MagicMock:
        predicate = page.expect_request_finished.call_args.args[0]
        assert predicate(request)
        assert not predicate(_request(url=url + "other"))
        assert not predicate(
            _request(
                b"action=join",
                url=url,
                content_type="application/x-www-form-urlencoded",
            )
        )
        events.append("armed")
        return signal

    def finish(*_args: object) -> None:
        nonlocal completed
        completed = True
        events.append("finished")

    def navigate(_locator: Locator) -> Response:
        nonlocal completed
        events.append("clicked")
        completed = completed_during_click
        return response

    def connected(attribute: str, value: str) -> None:
        assert completed
        assert events == ["armed", "clicked", "finished"]
        assert (attribute, value) == ("data-connection", "connected")

    signal.__enter__.side_effect = arm
    signal.__exit__.side_effect = finish
    assertion = MagicMock()
    assertion.to_have_attribute.side_effect = connected
    monkeypatch.setattr(join, "expect", MagicMock(return_value=assertion))
    monkeypatch.setattr(
        join, "evaluate_js", MagicMock(return_value={"connections": ["joining"]})
    )
    # When: the real helper executes with a controlled request-completion event.
    with join.room_join_readiness(page, url, tmp_path):
        navigate(page.locator('button[value="join"]'))
    # Then: the existing default timeout is used, with no evidence on success.
    assert "timeout" not in page.expect_request_finished.call_args.kwargs
    assert not list(tmp_path.glob("*-room-join-*.json"))


@pytest.mark.parametrize(
    ("action", "status"),
    [("status", 200), ("status", 403), ("resume", 200), ("audio_only", 200)],
)
def test_action_wait_requires_the_exact_completed_json_operation(
    monkeypatch: pytest.MonkeyPatch, action: str, status: int
) -> None:
    page = create_autospec(Page, instance=True)
    request = _request(
        f"action={action}".encode(), content_type="application/x-www-form-urlencoded"
    )
    response = create_autospec(Response, instance=True)
    monkeypatch.setattr(request, "existing_response", response)
    signal: MagicMock = page.expect_request_finished.return_value
    signal.value = request
    events: list[str] = []

    def subscribe() -> MagicMock:
        predicate = page.expect_request_finished.call_args.args[0]
        assert predicate(request)
        assert not predicate(
            _request(
                f"action={action}".encode(),
                url=STAFF_URL,
                content_type="application/x-www-form-urlencoded",
            )
        )
        assert not predicate(
            _request(b"action=join", content_type="application/x-www-form-urlencoded")
        )
        native = _request(
            f"action={action}".encode(),
            content_type="application/x-www-form-urlencoded",
        )
        monkeypatch.setattr(
            native,
            "headers",
            {
                "content-type": "application/x-www-form-urlencoded",
                "accept": "text/html",
            },
        )
        assert not predicate(native)
        events.append("subscribed")
        return signal

    def status_code() -> int:
        assert events == ["subscribed", "triggered", "finished"]
        return status

    signal.__enter__.side_effect = subscribe
    signal.__exit__.side_effect = lambda *_args: events.append("finished")
    monkeypatch.setattr(
        type(response), "status", PropertyMock(side_effect=status_code), raising=False
    )
    with join.room_action_finished(page, URL, action=action, status=status):
        events.append("triggered")
    assert events == ["subscribed", "triggered", "finished"]
    assert "timeout" not in page.expect_request_finished.call_args.kwargs


@pytest.mark.parametrize(
    "expiry",
    ["headers_only", "replacement_document", "already_expired"],
)
@pytest.mark.parametrize("state", ["expired", "ended", "failed", "removed"])
def test_expiry_wait_needs_no_finished_status_request(
    monkeypatch: pytest.MonkeyPatch,
    expiry: str,
    state: Literal["expired", "ended", "failed", "removed"],
) -> None:
    # Given: expiry can replace the document or already have disabled refresh.
    page = create_autospec(Page, instance=True, url=URL)
    page.expect_request_finished.side_effect = AssertionError(
        "No completed status request exists in this expiry flow"
    )
    page.expect_response.side_effect = AssertionError(
        "Expiry need not issue another request"
    )
    signal: MagicMock = page.expect_console_message.return_value
    events: list[str] = []
    expired = False

    def subscribe() -> MagicMock:
        predicate = page.expect_console_message.call_args.kwargs["predicate"]
        assert predicate(
            create_autospec(
                ConsoleMessage,
                instance=True,
                type="debug",
                text=f"clinic-room-state:{state}",
            )
        )
        assert not predicate(
            create_autospec(ConsoleMessage, instance=True, type="debug", text="other")
        )
        assert not predicate(
            create_autospec(
                ConsoleMessage,
                instance=True,
                type="error",
                text=f"clinic-room-state:{state}",
            )
        )
        events.append("subscribed")
        return signal

    def arm(_page: Page, _script: str) -> bool:
        nonlocal expired
        assert _page is page
        assert events == ["subscribed"]
        events.append("armed")
        expired = expiry == "already_expired"
        if expired:
            events.append("expired")
        return expired

    def refresh() -> None:
        nonlocal expired
        assert events == ["subscribed", "armed"]
        events.append("clicked")
        if expiry == "replacement_document":
            assert page.add_init_script.call_count == 1
        expired = True
        events.append("expired")

    def complete(*_args: object) -> None:
        assert expired
        events.append("observed")

    signal.__enter__.side_effect = subscribe
    signal.__exit__.side_effect = complete
    monkeypatch.setattr(join, "evaluate_js", arm)
    page.locator.return_value.click.side_effect = refresh
    # When: the real helper observes terminal state instead of a network body.
    join.refresh_room_state(page, state)
    # Then: a prearmed DOM signal, including an existing state, is sufficient.
    expected = ["subscribed", "armed"]
    if expiry != "already_expired":
        expected.append("clicked")
    assert events == [*expected, "expired", "observed"]
    page.expect_request_finished.assert_not_called()
    page.expect_response.assert_not_called()
    page.add_init_script.assert_called_once()
    assert "timeout" not in page.expect_console_message.call_args.kwargs


def test_expiry_wait_preserves_a_missing_terminal_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: refreshing neither expires the DOM nor produces its signal.
    page = create_autospec(Page, instance=True)
    original = PlaywrightError("synthetic missing expiry signal")
    page.expect_console_message.return_value.__exit__.side_effect = original
    monkeypatch.setattr(join, "evaluate_js", MagicMock(return_value=False))
    # When / Then: the original bounded failure cannot turn into success.
    with pytest.raises(PlaywrightError) as caught:
        join.refresh_room_state(page, "expired")
    assert caught.value is original


@pytest.mark.parametrize("kind", ["media", "offline", "track"])
@pytest.mark.parametrize("completed_during_trigger", [False, True])
def test_room_signals_subscribe_before_trigger_and_drain_the_event(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    *,
    completed_during_trigger: bool,
) -> None:
    page = create_autospec(Page, instance=True)
    signal: MagicMock = page.expect_console_message.return_value
    events: list[str] = []
    completed = False
    text = {
        "media": "clinic-room-media-settled:ready",
        "offline": "clinic-room-offline",
        "track": "clinic-room-track-ended",
    }[kind]

    def subscribe() -> MagicMock:
        predicate = page.expect_console_message.call_args.kwargs["predicate"]
        assert predicate(
            create_autospec(ConsoleMessage, instance=True, type="debug", text=text)
        )
        assert not predicate(
            create_autospec(ConsoleMessage, instance=True, type="error", text=text)
        )
        assert not predicate(
            create_autospec(ConsoleMessage, instance=True, type="debug", text="other")
        )
        events.append("subscribed")
        return signal

    def arm(_page: Page, script: str) -> None:
        assert _page is page
        assert events == ["subscribed"]
        assert (
            script
            == {
                "media": join.MEDIA_SETTLED,
                "offline": join.OFFLINE_SIGNAL,
                "track": join.TRACK_ENDED_SIGNAL,
            }[kind]
        )
        events.append("armed")

    def finish(*_args: object) -> None:
        nonlocal completed
        assert events == ["subscribed", "armed", "triggered"]
        completed = True
        events.append("finished")

    signal.__enter__.side_effect = subscribe
    signal.__exit__.side_effect = finish
    monkeypatch.setattr(join, "evaluate_js", arm)
    boundary = {
        "media": join.room_media_settled,
        "offline": join.room_offline,
        "track": join.room_track_ended,
    }[kind]
    with boundary(page):
        events.append("triggered")
        completed = completed_during_trigger
    assert completed
    assert events == ["subscribed", "armed", "triggered", "finished"]
    assert "timeout" not in page.expect_console_message.call_args.kwargs
    if kind == "media":
        page.add_init_script.assert_called_once_with(join.MEDIA_SETTLED)
    else:
        page.add_init_script.assert_not_called()


def test_device_retry_waits_for_acquisition_before_its_ready_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = create_autospec(Page, instance=True)
    events: list[str] = []
    signal: MagicMock = page.expect_console_message.return_value
    signal.__enter__.side_effect = lambda: events.append("subscribed")
    signal.__exit__.side_effect = lambda *_args: events.append("finished")
    monkeypatch.setattr(
        join,
        "evaluate_js",
        MagicMock(side_effect=lambda *_args: events.append("armed")),
    )
    page.locator.return_value.click.side_effect = lambda: events.append("retry")
    page.evaluate.side_effect = lambda *_args: events.append("end-track")
    assertion = MagicMock()

    def state(attribute: str, value: str) -> None:
        ended = ["subscribed", "armed", "end-track", "finished"]
        if (attribute, value) == ("data-media", "lost"):
            assert events == ended
        if (attribute, value) == ("data-media", "ready"):
            assert events == [*ended, "subscribed", "armed", "retry", "finished"]

    assertion.to_have_attribute.side_effect = state
    monkeypatch.setattr(patient_video, "expect", MagicMock(return_value=assertion))
    patient_video._lose_device(page)
    assert events == [
        "subscribed",
        "armed",
        "end-track",
        "finished",
        "subscribed",
        "armed",
        "retry",
        "finished",
    ]


def test_htmx_room_changes_wait_for_the_complete_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = create_autospec(Page, instance=True, url=STAFF_URL)
    request = _request(url=STAFF_URL)
    monkeypatch.setattr(request, "headers", {"hx-request": "true"})
    response = create_autospec(Response, instance=True)
    monkeypatch.setattr(request, "existing_response", response)
    signal: MagicMock = page.expect_request_finished.return_value
    signal.value = request
    events: list[str] = []

    def subscribe() -> MagicMock:
        predicate = page.expect_request_finished.call_args.args[0]
        assert predicate(request)
        foreign = _request()
        monkeypatch.setattr(foreign, "headers", {"hx-request": "true"})
        assert not predicate(foreign)
        get = _request(url=STAFF_URL, method="GET")
        monkeypatch.setattr(get, "headers", {"hx-request": "true"})
        assert not predicate(get)
        assert not predicate(_request(url=STAFF_URL))
        events.append("subscribed")
        return signal

    def status_code() -> int:
        assert events == ["subscribed", "clicked", "finished"]
        return 200

    signal.__enter__.side_effect = subscribe
    signal.__exit__.side_effect = lambda *_args: events.append("finished")
    page.locator.return_value.click.side_effect = lambda: events.append("clicked")
    monkeypatch.setattr(
        type(response), "status", PropertyMock(side_effect=status_code), raising=False
    )
    clinician._htmx(page, 'button[value="end"]')
    assert events == ["subscribed", "clicked", "finished"]
    assert "timeout" not in page.expect_request_finished.call_args.kwargs


def test_failure_retains_closed_state_without_forms_or_clinical_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a completed status reply contains fields that must never be evidence.
    page = create_autospec(Page, instance=True)
    page._impl_obj = MagicMock()
    request = _request()
    response = create_autospec(Response, instance=True, status=200)
    monkeypatch.setattr(request, "existing_response", response)
    monkeypatch.setattr(request, "timing", {**TIMING, "responseEnd": 7001.0})
    response._impl_obj = MagicMock()
    body = json.dumps(
        {
            "state": "waiting",
            "removed": False,
            "label": "private-label",
            "token": "private-token",
        }
    ).encode()
    monkeypatch.setattr(join, "_answer", MagicMock(side_effect=[[request], body]))
    monkeypatch.setattr(
        join,
        "evaluate_js",
        MagicMock(
            return_value={
                "surface": "room",
                "panel_count": 1,
                "connections": ["joining"],
                "transitions": {
                    "history": [{"from": "idle", "to": "joining"}],
                    "omitted": 0,
                },
            }
        ),
    )
    # When: failure evidence is saved after the assertion, not from callbacks.
    join.capture_room_failure(page, URL, tmp_path, "connected")
    # Then: state, HTTP outcome and timing survive; bodies/selectors/tokens do not.
    [path] = tmp_path.glob("patient-room-join-*.json")
    text = path.read_text()
    report = json.loads(text)
    assert report["requests"][0]["response_end_ms"] == 7001.0
    assert report["last_status_response"] == {
        "status": 200,
        "complete": True,
        "failed": False,
        "payload": {"state": "waiting", "removed": False},
    }
    assert report["document"]["connections"] == ["joining"]
    assert all(secret not in text for secret in ("private-label", "private-token", URL))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pending_status_body_is_not_awaited_for_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: headers arrived but the body is still pending.
    page = create_autospec(Page, instance=True)
    page._impl_obj = MagicMock()
    request = _request()
    monkeypatch.setattr(
        request,
        "existing_response",
        create_autospec(Response, instance=True, status=200),
    )
    answer = MagicMock(side_effect=[[request], AssertionError("pending body read")])
    monkeypatch.setattr(join, "_answer", answer)
    monkeypatch.setattr(
        join, "evaluate_js", MagicMock(return_value={"connections": ["joining"]})
    )
    # When: the failed join is captured.
    join.capture_room_failure(page, URL, tmp_path, "status")
    # Then: the pending state is explicit without waiting for or altering the reply.
    [path] = tmp_path.glob("patient-room-join-*.json")
    report = json.loads(path.read_text())
    assert report["last_status_response"] == {
        "status": 200,
        "complete": False,
        "failed": False,
    }
    answer.assert_called_once()


def test_diagnostic_storage_failure_preserves_the_original_join_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the original join fails and evidence storage also fails.
    page = create_autospec(Page, instance=True)
    original = PlaywrightError("synthetic join failure")
    monkeypatch.setattr(join, "capture_room_failure", MagicMock(side_effect=OSError))
    # When: the real failure path executes.
    with (
        pytest.raises(PlaywrightError) as caught,
        join.room_join_readiness(page, URL, tmp_path),
    ):
        raise original
    # Then: diagnostic failure never replaces the actual failure, even during cleanup.
    assert caught.value is original
    assert len(caught.value.__notes__) == 1
