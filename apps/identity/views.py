"""Password authentication and TOTP-protected identity screens."""

from django.conf import settings
from django.contrib.auth import login as session_login
from django.contrib.auth import logout as session_logout
from django.http import Http404, HttpRequest, HttpResponseBase
from django.shortcuts import render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.identity.forms import INVALID_LOGIN_MESSAGE, ClinicAuthenticationForm
from apps.identity.otp import (
    auth_redirect,
    auth_response,
    privileged_totp_required,
    safe_next_url,
)
from apps.identity.stepup import clear_step_up_verification


@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponseBase:
    """Create a password session through the hardened resolver backend."""
    target = safe_next_url(request, request.POST.get("next") or request.GET.get("next"))
    data = request.POST if request.method == "POST" else None
    form = ClinicAuthenticationForm(request=request, data=data)
    if request.method == "POST" and form.is_valid():
        clear_step_up_verification(request, clear_device=True)
        session_login(request, form.get_user())
        if "active_org_id" in request.session:
            return auth_redirect(request, target)
        session_logout(request)
        form.add_error(None, INVALID_LOGIN_MESSAGE)
    response = render(
        request,
        "identity/login.html",
        {"form": form, "next": target},
    )
    return auth_response(response)


@require_http_methods(["GET", "POST"])
def logout_view(request: HttpRequest) -> HttpResponseBase:
    """Require a CSRF-protected POST before flushing the authenticated session."""
    if request.method == "POST":
        session_logout(request)
        return auth_redirect(request, "/auth/login/")
    response = render(request, "identity/logout.html")
    return auth_response(response)


@privileged_totp_required
def protected_view(request: HttpRequest) -> HttpResponseBase:
    """Expose a compact enforcement target for authenticated product routes."""
    response = render(request, "identity/protected.html")
    return auth_response(response)


def auth_showcase(request: HttpRequest) -> HttpResponseBase:
    """Render inert authentication component states only in debug mode."""
    if not settings.DEBUG:
        raise Http404
    response = render(request, "identity/showcase.html")
    return auth_response(response)
