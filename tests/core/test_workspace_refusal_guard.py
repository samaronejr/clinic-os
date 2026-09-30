"""The collection guard checks both refusal channels without leaking values."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.core.workspace import ACTIVE_CLINIC_SESSION_KEY
from django.conf import settings
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import ResolverMatch, reverse

from workspace_refusal_support import RefusalGuardStats, check_refusal


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("channel", ["modified", "cookie"])
@pytest.mark.parametrize("view_name", ["workspace-command-run", "ui_api:agenda-query"])
def test_collection_guard_rejects_refusal_writes(
    status: int, channel: str, view_name: str
) -> None:
    request = RequestFactory().post(reverse(view_name))
    request.session = SessionStore()
    response = HttpResponse(status=status)
    if channel == "modified":
        request.session["synthetic"] = True
    else:
        response.set_cookie(settings.SESSION_COOKIE_NAME, "synthetic")
    stats = RefusalGuardStats()
    with pytest.raises(pytest.fail.Exception):
        check_refusal(request, response, stats)
    assert stats.responses == 1
    assert stats.refusals == 1
    assert stats.violations == 1


@pytest.mark.parametrize("status", [200, 302, 400, 403, 404, 405])
def test_collection_guard_accepts_read_only_responses(status: int) -> None:
    request = RequestFactory().get(reverse("workspace-command"))
    request.session = SessionStore()
    stats = RefusalGuardStats()
    check_refusal(request, HttpResponse(status=status), stats)
    assert stats.responses == 1
    assert stats.violations == 0


@pytest.mark.parametrize("scope", ["authentication", "parameter", "session"])
def test_collection_guard_derives_clinic_scope_for_extension_routes(scope: str) -> None:
    request = RequestFactory().get("/__synthetic__/refusal/")
    request.session = SessionStore()
    request.session["authentication-freshness"] = True
    request.resolver_match = ResolverMatch(
        lambda _: HttpResponse(),
        (),
        {"clinic_id": uuid4()} if scope == "parameter" else {},
        url_name="synthetic-refusal",
    )
    if scope == "session":
        request.session[ACTIVE_CLINIC_SESSION_KEY] = str(uuid4())
    response = HttpResponse(status=403)
    response.set_cookie(settings.SESSION_COOKIE_NAME, "synthetic")
    stats = RefusalGuardStats()
    if scope == "authentication":
        check_refusal(request, response, stats)
        assert stats.refusals == 0
        assert stats.violations == 0
    else:
        with pytest.raises(pytest.fail.Exception):
            check_refusal(request, response, stats)
        assert stats.refusals == 1
        assert stats.violations == 1
    assert stats.responses == 1
