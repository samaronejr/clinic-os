from __future__ import annotations

from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, urlsplit

import pytest
from apps.identity import stepup
from apps.identity.forms import INVALID_CODE_MESSAGE
from apps.identity.stepup import STEP_UP_SESSION_KEY
from django.contrib.auth import SESSION_KEY
from django.test import Client, override_settings
from django_otp import DEVICE_ID_SESSION_KEY

from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    runtime_role,
    token_for,
)
from stepup_test_support import (
    STEP_UP_MAX_AGE,
    STEP_UP_NOW,
    clear_freshness,
    logged_in_client,
    seed_freshness,
)

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_fixed_step_up_clock"),
]

SENSITIVE_VALUE_CANARY: Final = "123456789012-step-up-sensitive-canary"


@pytest.fixture
def _fixed_step_up_clock(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)


@override_settings(ROOT_URLCONF="stepup_urls")
def test_stale_decorator_redirects_to_named_tenant_bound_challenge(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device, verified_at=STEP_UP_NOW - STEP_UP_MAX_AGE - 1)

    with runtime_role():
        response = client.get("/__test__/issuance/")
        challenge = client.get(response.headers["Location"])

    parsed = urlsplit(response.headers["Location"])
    assert response.status_code == 302
    assert parsed.path == "/auth/step-up/"
    assert parse_qs(parsed.query) == {"next": ["/__test__/issuance/"]}
    assert challenge.status_code == 200
    assert b"Confirm this sensitive action" in challenge.content
    assert challenge.headers["Cache-Control"].startswith("no-cache, no-store")
    assert "HX-Request" in challenge.headers["Vary"]


@override_settings(ROOT_URLCONF="stepup_urls")
def test_htmx_stale_request_uses_success_status_and_hx_redirect(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device, verified_at=STEP_UP_NOW - STEP_UP_MAX_AGE - 1)

    with runtime_role():
        response = client.get(
            "/__test__/issuance/",
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"].startswith("/auth/step-up/")


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize(
    "hostile_target",
    [
        "https://attacker.invalid/steal",
        "//attacker.invalid/steal",
        "/%61uth/step-up/",
        "/auth/step-up",
    ],
)
def test_challenge_rejects_hostile_or_recursive_next_targets(
    rbac_graph: RbacGraph,
    hostile_target: str,
) -> None:
    create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)

    with runtime_role():
        response = client.get("/auth/step-up/", {"next": hostile_target})

    assert response.status_code == 200
    assert b'name="next" value="/auth/protected/"' in response.content


@override_settings(ROOT_URLCONF="stepup_urls")
def test_exact_device_reverification_rotates_session_stamps_time_and_unlocks_hook(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    old_key = client.session.session_key

    with runtime_role(), fixed_otp_time():
        response = client.post(
            "/auth/step-up/",
            {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
                "next": "/__test__/issuance/",
            },
        )
        protected = client.get("/__test__/issuance/")

    assert response.status_code == 302
    assert response.headers["Location"] == "/__test__/issuance/"
    assert client.session.session_key != old_key
    assert client.session[STEP_UP_SESSION_KEY] == STEP_UP_NOW
    assert client.session[DEVICE_ID_SESSION_KEY] == device.persistent_id
    assert protected.status_code == 200


@override_settings(ROOT_URLCONF="stepup_urls")
def test_replay_invalid_and_throttled_attempts_never_refresh_freshness(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    token = token_for(device)

    with runtime_role(), fixed_otp_time():
        first = client.post(
            "/auth/step-up/",
            {"otp_device": device.persistent_id, "otp_token": token},
        )
        clear_freshness(client)
        replay = client.post(
            "/auth/step-up/",
            {"otp_device": device.persistent_id, "otp_token": token},
        )
        invalid = client.post(
            "/auth/step-up/",
            {"otp_device": device.persistent_id, "otp_token": "not-a-code"},
        )
        throttled = client.post(
            "/auth/step-up/",
            {"otp_device": device.persistent_id, "otp_token": "not-a-code"},
        )

    assert first.status_code == 302
    assert replay.status_code == invalid.status_code == throttled.status_code == 200
    assert STEP_UP_SESSION_KEY not in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
def test_htmx_invalid_form_stays_200_without_redirect(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    headers = {"HX-Request": "true"}

    with runtime_role():
        invalid = client.post(
            "/auth/step-up/",
            {"otp_device": device.persistent_id, "otp_token": "not-a-code"},
            headers=headers,
        )

    assert invalid.status_code == 200
    assert "HX-Redirect" not in invalid.headers


@override_settings(ROOT_URLCONF="stepup_urls")
def test_htmx_success_uses_safe_redirect_header(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)

    with runtime_role(), fixed_otp_time():
        success = client.post(
            "/auth/step-up/",
            {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
                "next": "/__test__/issuance/",
            },
            headers={"HX-Request": "true"},
        )

    assert success.status_code == 204
    assert success.headers["HX-Redirect"] == "/__test__/issuance/"


@override_settings(ROOT_URLCONF="stepup_urls")
def test_step_up_post_requires_csrf_and_does_not_create_freshness(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = Client(enforce_csrf_checks=True)
    user_client = logged_in_client(rbac_graph.physician)
    client.cookies = user_client.cookies

    with runtime_role(), fixed_otp_time():
        response = client.post(
            "/auth/step-up/",
            {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
            },
        )

    assert response.status_code == 403
    assert STEP_UP_SESSION_KEY not in client.session
    assert SESSION_KEY in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
def test_invalid_token_is_absent_from_html_logs_and_session(
    rbac_graph: RbacGraph,
    caplog: LogCaptureFixture,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)

    with runtime_role():
        response = client.post(
            "/auth/step-up/",
            {
                "otp_device": device.persistent_id,
                "otp_token": SENSITIVE_VALUE_CANARY,
            },
        )

    assert response.status_code == 200
    assert INVALID_CODE_MESSAGE.encode() in response.content
    assert SENSITIVE_VALUE_CANARY.encode() not in response.content
    assert SENSITIVE_VALUE_CANARY not in caplog.text
    assert STEP_UP_SESSION_KEY not in client.session
