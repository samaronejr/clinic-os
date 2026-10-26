"""A request still carrying a pre-rotation session key never logs the user out.

OTP verification (sign-in and step-up) rotates the session key with
``cycle_key()``. A request the browser sent before the rotation - the service
worker's ``/sw.js`` update check, a status poll - reaches the server after it
carrying the old key. That request is refused, but it must not answer with a
``Set-Cookie`` deleting the session cookie: the browser would apply it to the
freshly rotated cookie and silently sign the user out.

The interleavings are forced, not timed: requests either arrive after rotation
or pause with the old payload loaded before inventory admission. Their response
cookies are applied to the signed-in client's jar as a browser applies them.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from typing import TYPE_CHECKING

import pytest
from apps.identity import otp
from apps.identity.models import SessionRecord, User
from apps.identity.session_inventory import (
    record_verified_session,
    revoke_session,
    session_hash,
    session_is_active,
)
from apps.tenancy import middleware as tenant_middleware
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import DatabaseError, connections
from django.test import Client, override_settings

from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    runtime_role,
    token_for,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest
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


def _concurrent_stale_request(key: str, path: str) -> _MonkeyPatchedWSGIResponse:
    """Own the runtime connection on the independent request's worker thread."""
    try:
        with runtime_role():
            return _stale_request(key, path)
    finally:
        connections.close_all()


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


@pytest.mark.parametrize("save_every_request", [False, True])
@pytest.mark.parametrize("path", ["/auth/protected/", "/api/ui/v1/agenda/query/"])
def test_preloaded_request_after_rotation_keeps_the_rotated_session(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    path: str,
    *,
    save_every_request: bool,
) -> None:
    # Given an authenticated payload loaded before the inventory decision.
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    browser = Client()
    loaded, resume = Event(), Event()
    coordinator = current_thread()
    active = session_is_active
    ordering: list[str] = []

    def paused_inventory(*, user_id: UUID, session_key: str) -> bool:
        if current_thread() is not coordinator:
            ordering.append("old-payload-loaded")
            loaded.set()
            assert resume.wait(timeout=10), "rotation did not release admission"
            ordering.append("old-inventory-after-rotation")
        return active(user_id=user_id, session_key=session_key)

    monkeypatch.setattr(tenant_middleware, "session_is_active", paused_inventory)
    with (
        override_settings(SESSION_SAVE_EVERY_REQUEST=save_every_request),
        runtime_role(),
    ):
        assert browser.login(username=username, password=RBAC_RAW_CREDENTIAL)
        old = browser.cookies[COOKIE].value
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(_concurrent_stale_request, old, path)
            try:
                assert loaded.wait(timeout=10), "old payload was not loaded"
                # When real OTP verification completes before stale admission.
                with fixed_otp_time():
                    verified = browser.post(
                        "/auth/verify/",
                        {
                            "otp_device": device.persistent_id,
                            "otp_token": token_for(device),
                        },
                        headers={"HX-Request": "true"},
                    )
                assert verified.status_code == 204
                rotated = browser.cookies[COOKIE].value
                assert rotated != old
                replacement_before = Session.objects.get(session_key=rotated)
                ordering.append("verify-response-applied")
            finally:
                resume.set()
            stale = pending.result(timeout=10)
        replacement_after = Session.objects.get(session_key=rotated)
        replacement_unchanged = (
            hashlib.sha256(replacement_before.session_data.encode()).digest()
            == hashlib.sha256(replacement_after.session_data.encode()).digest()
            and replacement_before.expire_date == replacement_after.expire_date
        )
        assert replacement_unchanged
        assert not Session.objects.filter(session_key=old).exists()
        # Then refusal cannot delete or overwrite the browser's new cookie.
        assert ordering == [
            "old-payload-loaded",
            "verify-response-applied",
            "old-inventory-after-rotation",
        ]
        assert stale.status_code == (403 if path.startswith("/api/") else 302)
        before = browser.get("/auth/protected/").status_code
        browser.cookies.update(stale.cookies)
        after = browser.get("/auth/protected/").status_code
        request.node.user_properties.append(
            (
                "cookie_provenance",
                {
                    "verify": verified.status_code,
                    "old_response": stale.status_code,
                    "old_response_cookie": COOKIE in stale.cookies,
                    "old_response_deletes": (
                        COOKIE in stale.cookies
                        and str(stale.cookies[COOKIE]["max-age"]) == "0"
                    ),
                    "replacement_row_unchanged": replacement_unchanged,
                    "protected_before_late_response": before,
                    "protected_after_late_response": after,
                    "rotated_cookie_preserved": (
                        browser.cookies[COOKIE].value == rotated
                    ),
                },
            )
        )
        assert COOKIE not in stale.cookies
        assert browser.cookies[COOKIE].value == rotated
        assert before == after == OK
        assert _stale_request(old, "/auth/protected/").status_code != OK


@pytest.mark.parametrize("failure", ["cycle-key", "inventory"])
def test_failed_rotation_never_publishes_a_verified_cookie(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Given a real password session with no verified factor.
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    browser = Client(raise_request_exception=False)
    with runtime_role():
        assert browser.login(username=username, password=RBAC_RAW_CREDENTIAL)
        old = browser.cookies[COOKIE].value
        if failure == "cycle-key":

            def failed_cycle(store: SessionStore) -> None:
                raise DatabaseError

            monkeypatch.setattr(SessionStore, "cycle_key", failed_cycle)
        else:
            original = record_verified_session

            def failed_inventory(
                *, request: HttpRequest, device_id: int, old_key: str | None
            ) -> None:
                original(request=request, device_id=device_id, old_key=old_key)
                raise DatabaseError

            monkeypatch.setattr(otp, "record_verified_session", failed_inventory)
        # When the actual transaction fails before or after the inventory writes.
        with fixed_otp_time():
            response = browser.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            )
        # Then no replacement cookie or verified authority escapes rollback.
        assert response.status_code == 500
        assert COOKIE not in response.cookies
        assert browser.cookies[COOKIE].value == old
        assert session_is_active(user_id=rbac_graph.physician, session_key=old)
        protected = browser.get("/auth/protected/")
        assert protected.status_code == 302
        assert protected.headers["Location"].startswith("/auth/verify/")


@pytest.mark.parametrize("save_every_request", [False, True])
def test_existing_row_revocation_still_clears_the_cookie(
    rbac_graph: RbacGraph, *, save_every_request: bool
) -> None:
    # Given a real authenticated inventory entry with its backend payload intact.
    user = User.objects.get(pk=rbac_graph.physician)
    browser = Client()
    with (
        override_settings(SESSION_SAVE_EVERY_REQUEST=save_every_request),
        runtime_role(),
    ):
        assert browser.login(username=user.username, password=RBAC_RAW_CREDENTIAL)
        key = browser.cookies[COOKIE].value
        with tenant_context(rbac_graph.physician, rbac_graph.organization_a):
            record = SessionRecord.objects.get(session_key_hash=session_hash(key))
            revoke_session(session_record_id=record.pk)
        assert Session.objects.filter(session_key=key).exists()
        assert not session_is_active(user_id=user.pk, session_key=key)
        # When that revoked, still-present row reaches the actual staff boundary.
        response = browser.get("/auth/protected/")
        # Then legitimate revocation retains logout, deletion and old-key denial.
        assert response.status_code == 302
        assert str(response.cookies[COOKIE]["max-age"]) == "0"
        assert response.cookies[COOKIE].value == ""
        assert not Session.objects.filter(session_key=key).exists()
        assert _stale_request(key, "/auth/protected/").status_code != OK
        assert not session_is_active(user_id=user.pk, session_key=key)
