"""Primary SSO and masked support authenticate their own sessionless entrypoints."""

from __future__ import annotations

from uuid import UUID

from django.contrib.auth import login
from django.http import HttpRequest, HttpResponseBase, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.identity.enterprise_access import support_diagnostics
from apps.identity.enterprise_authority import GovernanceDeniedError
from apps.identity.enterprise_sso import begin_sso, finish_sso
from apps.identity.otp import auth_response
from apps.identity.support_presenter import support_context

SUPPORT_TEMPLATE = "identity/support_diagnostics.html"


@require_POST
def sso_begin(request: HttpRequest) -> HttpResponseBase:
    """Start a CSRF-protected server-side state/nonce/PKCE ceremony."""
    try:
        clinic_id = UUID(request.POST.get("clinic_id", ""))
        options = begin_sso(clinic_id=clinic_id, session=request.session)
    except (ValueError, GovernanceDeniedError):
        return auth_response(JsonResponse({"code": "access_denied"}, status=403))
    return auth_response(JsonResponse(options))


@require_POST
def sso_finish(request: HttpRequest) -> HttpResponseBase:
    """Authenticate primary identity without bypassing OTP/passkey enrollment."""
    try:
        result = finish_sso(
            state=request.POST.get("state", ""),
            code=request.POST.get("code", ""),
            session=request.session,
        )
    except GovernanceDeniedError:
        return auth_response(JsonResponse({"code": "access_denied"}, status=403))
    login(request, result.user, backend="apps.identity.auth_backends.ClinicBackend")
    request.session["active_org_id"] = str(result.organization_id)
    # Never stamp OTP freshness or mark a passkey verified from IdP AMR/ACR claims.
    return auth_response(redirect("identity:enroll"))


@require_http_methods(["GET", "POST"])
def support_diagnostics_view(request: HttpRequest) -> HttpResponseBase:
    """Use only the signed browser session's own key, not a posted user or key.

    GET shows the empty selector form and reads nothing. POST carries the clinic
    and grant selectors in the body; a missing session, malformed selector and
    every refused grant get the same 403 page.
    """
    if request.method == "GET":
        return auth_response(render(request, SUPPORT_TEMPLATE, {"state": "form"}))
    key = request.session.session_key
    try:
        result = (
            None
            if key is None
            else support_diagnostics(
                clinic_id=UUID(request.POST.get("clinic_id", "")),
                grant_id=UUID(request.POST.get("grant_id", "")),
                session_key=key,
            )
        )
    except (ValueError, GovernanceDeniedError):
        result = None
    if result is None:
        return auth_response(
            render(request, SUPPORT_TEMPLATE, {"state": "denied"}, status=403)
        )
    return auth_response(
        render(
            request,
            SUPPORT_TEMPLATE,
            {"state": "ready", **support_context(result)},
        )
    )
