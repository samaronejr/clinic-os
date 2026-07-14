from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity import stepup
from apps.identity.models import User
from apps.identity.stepup import (
    STEP_UP_SESSION_KEY,
    StepUpRequired,
    assert_step_up,
    require_recent_verification,
)
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from stepup_test_support import STEP_UP_MAX_AGE, STEP_UP_NOW

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture
def verified_request(monkeypatch: MonkeyPatch) -> HttpRequest:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)
    user = User(id=uuid4(), username="step-up-acceptance")
    device = TOTPDevice(id=1, user=user, confirmed=True)
    user.__dict__["otp_device"] = device
    user.__dict__["is_verified"] = lambda: True
    request = RequestFactory().get("/sensitive-action/")
    session = SessionStore()
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session[STEP_UP_SESSION_KEY] = STEP_UP_NOW
    request.session = session
    request.user = user
    return request


def test_fresh_verification_passes_the_sensitive_boundary(
    verified_request: HttpRequest,
) -> None:
    assert_step_up(verified_request, max_age=STEP_UP_MAX_AGE)

    assert verified_request.session[STEP_UP_SESSION_KEY] == STEP_UP_NOW


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_older_than_max_age_redirects_to_reverification(
    verified_request: HttpRequest,
) -> None:
    verified_request.session[STEP_UP_SESSION_KEY] = STEP_UP_NOW - STEP_UP_MAX_AGE - 1

    @require_recent_verification(max_age=STEP_UP_MAX_AGE)
    def sensitive_action(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("sensitive action reached")

    response = sensitive_action(verified_request)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/step-up/")
    assert STEP_UP_SESSION_KEY not in verified_request.session


def test_assert_step_up_rejects_stale_verification(
    verified_request: HttpRequest,
) -> None:
    verified_request.session[STEP_UP_SESSION_KEY] = STEP_UP_NOW - STEP_UP_MAX_AGE - 1

    with pytest.raises(StepUpRequired):
        assert_step_up(verified_request, max_age=STEP_UP_MAX_AGE)

    assert STEP_UP_SESSION_KEY not in verified_request.session
