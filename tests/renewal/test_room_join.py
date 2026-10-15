"""DB-free checks for the room join's event boundary and closed failure evidence."""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING, Final
from unittest.mock import MagicMock, create_autospec

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request, Response

from renewal.browser import _room_join as join

if TYPE_CHECKING:
    from pathlib import Path

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
    assert caught.value.__notes__ == ["Join evidence write failed: OSError"]
