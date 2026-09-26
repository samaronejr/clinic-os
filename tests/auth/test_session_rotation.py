"""A request still carrying a pre-rotation session key never logs the user out.

OTP verification (sign-in and step-up) rotates the session key with
``cycle_key()``. A request the browser sent before the rotation - the service
worker's ``/sw.js`` update check, a status poll - reaches the server after it
carrying the old key. That request is refused, but it must not answer with a
``Set-Cookie`` deleting the session cookie: the browser would apply it to the
freshly rotated cookie and silently sign the user out.

The interleaving is forced, not timed: the stale request is issued only after
the rotation completed, and its response cookies are applied to the signed-in
client's jar exactly as a browser applies them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from django.conf import settings
from django.test import Client

from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    runtime_role,
    token_for,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

COOKIE = settings.SESSION_COOKIE_NAME
FORBIDDEN = 403
OK = 200


def _stale_request(stale_key: str, path: str) -> _MonkeyPatchedWSGIResponse:
    """Send ``path`` from a second client holding only the pre-rotation key."""
    worker = Client()
    worker.cookies[COOKIE] = stale_key
    return worker.get(path)


def test_stale_service_worker_fetch_after_rotation_keeps_the_rotated_session(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    browser = Client()
    with runtime_role():
        assert browser.login(username=username, password=RBAC_RAW_CREDENTIAL)
        # The cookie the browser held when the verify page loaded; the
        # worker's update check leaves with it.
        pre_rotation = browser.cookies[COOKIE].value
        with fixed_otp_time():
            verified = browser.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            )
        assert verified.status_code == 302
        rotated = browser.cookies[COOKIE].value
        assert rotated != pre_rotation

        # The in-flight update check lands after the rotation.
        stale = _stale_request(pre_rotation, "/sw.js")
        assert stale.status_code == FORBIDDEN
        assert COOKIE not in stale.cookies
        browser.cookies.update(stale.cookies)

        # The rotated cookie survives and the next page is authorized.
        assert browser.cookies[COOKIE].value == rotated
        assert browser.get("/auth/protected/").status_code == OK
        assert browser.get("/sw.js").status_code == OK


def test_pre_rotation_cookie_is_rejected_after_rotation(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    browser = Client()
    with runtime_role():
        assert browser.login(username=username, password=RBAC_RAW_CREDENTIAL)
        pre_rotation = browser.cookies[COOKIE].value
        with fixed_otp_time():
            verified = browser.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            )
        assert verified.status_code == 302
        assert browser.get("/auth/protected/").status_code == OK

        # Session fixation stays closed: the old key authorizes nothing.
        for path in ("/auth/protected/", "/sw.js"):
            refused = _stale_request(pre_rotation, path)
            assert refused.status_code != OK, path
            assert COOKIE not in refused.cookies, path
        verify = _stale_request(pre_rotation, "/auth/verify/")
        assert verify.status_code != OK


def test_logout_still_deletes_the_session_cookie(rbac_graph: RbacGraph) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    browser = Client()
    with runtime_role():
        assert browser.login(username=username, password=RBAC_RAW_CREDENTIAL)
        with fixed_otp_time():
            browser.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            )
        signed_in = browser.cookies[COOKIE].value
        response = browser.post("/auth/logout/")
        morsel = response.cookies[COOKIE]
        assert morsel.value == ""
        assert str(morsel["max-age"]) == "0"
        assert browser.get("/auth/protected/").status_code != OK
        assert _stale_request(signed_in, "/auth/protected/").status_code != OK
