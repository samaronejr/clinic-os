"""Tenant-bound TOTP enrollment and verification views."""

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
    get_or_create_pending_device,
    is_confirmed_verified_user,
    is_privileged_user,
    pending_devices,
    provisioning_qr_data_uri,
    record_otp_verification,
    safe_next_url,
)


def _current_user(request: HttpRequest) -> User | None:
    user = request.user
    if not isinstance(user, User) or not user.is_authenticated:
        session_logout(request)
        return None
    return user


@sensitive_variables()
def _render_enrollment(
    request: HttpRequest,
    *,
    form: ExplicitOTPTokenForm | None,
    qr_data_uri: str | None,
    target: str,
) -> HttpResponseBase:
    response = render(
        request,
        "identity/enroll.html",
        {
            "form": form,
            "next": target,
            "qr_data_uri": qr_data_uri,
            "stage": "scan" if form is not None else "intro",
        },
    )
    return auth_response(response)


@sensitive_variables()
@sensitive_post_parameters("otp_token")
@require_http_methods(["GET", "POST"])
def enroll_view(request: HttpRequest) -> HttpResponseBase:
    """Enroll exactly one pending TOTP device for a privileged user."""
    target = safe_next_url(request, request.POST.get("next") or request.GET.get("next"))
    user = _current_user(request)
    if user is None or not is_privileged_user(user):
        return (
            flow_redirect(request, "identity:login", target)
            if user is None
            else auth_redirect(request, target)
        )
    if confirmed_devices(user.pk):
        return (
            auth_redirect(request, target)
            if is_confirmed_verified_user(user)
            else flow_redirect(request, "identity:verify", target)
        )
    if request.method == "GET":
        return _render_enrollment(
            request,
            form=None,
            qr_data_uri=None,
            target=target,
        )

    action = request.POST.get("action")
    if action == "start":
        device = get_or_create_pending_device(user.pk)
        form = ExplicitOTPTokenForm(user, [device], request=request)
        return _render_enrollment(
            request,
            form=form,
            qr_data_uri=provisioning_qr_data_uri(device, user.get_username()),
            target=target,
        )
    devices = list(pending_devices(user.pk))
    form = ExplicitOTPTokenForm(
        user,
        devices,
        request=request,
        data=request.POST,
    )
    is_confirmation = action == "confirm" or bool(request.POST.get("otp_device"))
    if is_confirmation and form.is_valid():
        verified_device = form.selected_device()
        if verified_device is not None and not verified_device.confirmed:
            verified_device.confirmed = True
            verified_device.save(update_fields=("confirmed",))
            record_otp_verification(request, verified_device)
            return auth_redirect(request, target)
    qr_data_uri = (
        provisioning_qr_data_uri(devices[0], user.get_username()) if devices else None
    )
    return _render_enrollment(
        request,
        form=form,
        qr_data_uri=qr_data_uri,
        target=target,
    )


@sensitive_variables()
@sensitive_post_parameters("otp_token")
@require_http_methods(["GET", "POST"])
def verify_view(request: HttpRequest) -> HttpResponseBase:
    """Verify one explicit confirmed device and rotate the password session."""
    target = safe_next_url(request, request.POST.get("next") or request.GET.get("next"))
    user = _current_user(request)
    if user is None:
        return flow_redirect(request, "identity:login", target)
    if not is_privileged_user(user):
        return auth_redirect(request, target)
    devices = list(confirmed_devices(user.pk))
    if not devices:
        return flow_redirect(request, "identity:enroll", target)
    if is_confirmed_verified_user(user):
        return auth_redirect(request, target)
    data = request.POST if request.method == "POST" else None
    form = ExplicitOTPTokenForm(user, devices, request=request, data=data)
    if request.method == "POST" and form.is_valid():
        device = form.selected_device()
        if device is not None:
            record_otp_verification(request, device)
            return auth_redirect(request, target)
    response = render(
        request,
        "identity/verify.html",
        {"form": form, "next": target},
    )
    return auth_response(response)
