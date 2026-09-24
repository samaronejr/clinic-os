"""Tenant-bound recent-verification challenge views."""

from django.contrib.auth import logout as session_logout
from django.http import HttpRequest, HttpResponseBase
from django.shortcuts import render
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from apps.identity.forms import ExplicitOTPTokenForm
from apps.identity.models import User
from apps.identity.otp import (
    auth_redirect,
    auth_response,
    confirmed_devices,
    flow_redirect,
    record_otp_verification,
    safe_next_url,
)
from apps.identity.stepup import (
    StepUpRequired,
    assert_step_up,
    clear_step_up_intent,
    step_up_intent_for,
)


def _current_active_user(request: HttpRequest) -> User | None:
    user = request.user
    if not isinstance(user, User) or not user.is_authenticated or not user.is_active:
        session_logout(request)
        return None
    return user


@sensitive_variables()
@sensitive_post_parameters("otp_token")
@require_http_methods(["GET", "POST"])
def step_up_view(request: HttpRequest) -> HttpResponseBase:
    """Re-verify one exact confirmed device before a sensitive operation."""
    target = safe_next_url(request, request.POST.get("next") or request.GET.get("next"))
    user = _current_active_user(request)
    if user is None:
        return flow_redirect(request, "identity:login", target)

    freshness_is_valid = True
    try:
        assert_step_up(request)
    except StepUpRequired:
        freshness_is_valid = False
    if freshness_is_valid:
        clear_step_up_intent(request)
        return auth_redirect(request, target)

    devices = list(confirmed_devices(user.pk))
    if not devices:
        return auth_response(render(request, "403.html", status=403))

    data = request.POST if request.method == "POST" else None
    form = ExplicitOTPTokenForm(user, devices, request=request, data=data)
    if request.method == "POST" and form.is_valid():
        device = form.selected_device()
        if device is not None:
            record_otp_verification(request, device)
            clear_step_up_intent(request)
            return auth_redirect(request, target)

    # The intent is display-only context for the action that sent the user
    # here; it is shown only for its own continuation target.
    response = render(
        request,
        "identity/step_up.html",
        {
            "form": form,
            "next": target,
            "intent": step_up_intent_for(request, target),
        },
    )
    return auth_response(response)
