"""Private invitation enrollment and separately authenticated delivery callback."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth import authenticate, login
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST

from apps.comms.adapters import CallbackAuthenticationError
from apps.core.integration import receive_provider_callback
from apps.identity.invitations import EMAIL_PROVIDER, enroll_invitation
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.otp import (
    auth_redirect,
    auth_response,
    provisioning_qr_data_uri,
    record_otp_verification,
)
from apps.identity.staff_forms import InvitationEnrollmentForm

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase


@sensitive_variables()
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def invitation_view(request: HttpRequest) -> HttpResponseBase:
    """Stage password and seed privately; log in only after confirmed TOTP."""
    form = InvitationEnrollmentForm(request.POST if request.method == "POST" else None)
    qr_data_uri = None
    if request.method == "POST" and form.is_valid():
        username: str = form.cleaned_data["username"]
        password: str = form.cleaned_data["password"]
        token: str = form.cleaned_data["invitation_code"]
        otp_token: str = form.cleaned_data["otp_token"]
        with transaction.atomic(durable=True):
            try:
                enrollment = enroll_invitation(
                    token=token,
                    username=username,
                    password=password,
                    otp_token=otp_token or None,
                )
            except (LifecycleDeniedError, ValidationError):
                form.add_error(
                    None,
                    _(
                        "We could not accept this invitation. "
                        "Check the private details."
                    ),
                )
            else:
                if enrollment.accepted_user_id is not None:
                    user = authenticate(request, username=username, password=password)
                    if user is None or user.pk != enrollment.accepted_user_id:
                        raise LifecycleDeniedError
                    login(request, user)
                    record_otp_verification(request, enrollment.device)
                    return auth_redirect(request, reverse("workspace-home"))
                qr_data_uri = provisioning_qr_data_uri(enrollment.device, username)
                if otp_token:
                    form.add_error("otp_token", _("Enter a valid authenticator code."))
    return auth_response(
        render(
            request,
            "identity/invitation.html",
            {"form": form, "qr_data_uri": qr_data_uri},
            status=400 if form.errors else 200,
        )
    )


@csrf_exempt
@require_POST
def identity_notification_callback(request: HttpRequest) -> HttpResponse:
    """Authenticate exact raw receipt bytes before any operation/tenant lookup."""
    try:
        result = receive_provider_callback(
            provider=EMAIL_PROVIDER, headers=request.headers, body=request.body
        )
    except CallbackAuthenticationError:
        return HttpResponse(status=403)
    return HttpResponse(status=200 if result in {"applied", "duplicate"} else 400)
