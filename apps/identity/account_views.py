"""Native private recovery and user-owned session inventory screens."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth import logout
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST

from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.models import RecoveryBatch, UserClinicRole
from apps.identity.otp import auth_redirect, auth_response, flow_redirect
from apps.identity.recovery import issue_recovery_codes, recover_authenticator
from apps.identity.security_forms import RecoveryCodeForm, SessionRevocationForm
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
    own_sessions,
    revoke_session,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


def request_session_key(request: HttpRequest) -> str:
    """Parse the authenticated framework key at the HTTP boundary only."""
    key = request.session.session_key
    if key is None:
        raise SessionInventoryDeniedError
    return key


@require_http_methods(["GET", "POST"])
def sessions_view(request: HttpRequest) -> HttpResponseBase:
    """List minimized own records and revoke through a body-only selector."""
    form = SessionRevocationForm(request.POST if request.method == "POST" else None)
    revoked = False
    if request.method == "POST" and form.is_valid():
        selected: UUID = form.cleaned_data["session_id"]
        revoke_session(session_record_id=selected)
        revoked = True
    return auth_response(
        render(
            request,
            "identity/sessions.html",
            {"sessions": own_sessions(), "form": form, "revoked": revoked},
            status=400 if form.errors else 200,
        )
    )


@sensitive_variables()
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def recovery_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Let a password-authenticated user consume a private recovery code."""
    try:
        require_current_actor_clinic_roles(
            clinic_id=clinic_id, roles=tuple(UserClinicRole.Role)
        )
    except CurrentActorError as error:
        raise LifecycleDeniedError from error
    form = RecoveryCodeForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        code: str = form.cleaned_data["code"]
        if recover_authenticator(clinic_id=clinic_id, code=code):
            logout(request)
            return auth_redirect(request, reverse("identity:login"))
        form.add_error(
            None,
            _(
                "That recovery code is unavailable. "
                "Use another code or ask your clinic administrator."
            ),
        )
    enrolled = RecoveryBatch.objects.filter(
        user_id=current_actor_id(), revoked_at__isnull=True
    ).exists()
    return auth_response(
        render(
            request,
            "identity/recovery.html",
            {"clinic_id": clinic_id, "form": form, "enrolled": enrolled},
            status=400 if form.errors else 200,
        )
    )


@sensitive_variables()
@require_POST
def recovery_enrollment(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Show newly issued plaintext once, never in storage or the next GET."""
    target = reverse("identity:recovery", kwargs={"clinic_id": clinic_id})
    try:
        issued = issue_recovery_codes(
            clinic_id=clinic_id, session_key=request_session_key(request)
        )
    except SessionInventoryDeniedError:
        return flow_redirect(request, "identity:step-up", target)
    except (LifecycleDeniedError, CurrentActorError):
        return auth_response(render(request, "403.html", status=403))
    return auth_response(
        render(
            request,
            "identity/recovery_codes.html",
            {"codes": issued.codes, "return_href": target},
        )
    )
