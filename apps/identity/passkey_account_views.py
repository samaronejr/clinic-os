"""Native own passkey management, with explicit organization policy opt-in."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from apps.identity.account_views import request_session_key
from apps.identity.current_context import current_actor_id
from apps.identity.models import Clinic, Organization
from apps.identity.otp import auth_redirect, auth_response, privileged_totp_required
from apps.identity.passkey_challenges import PasskeyDeniedError
from apps.identity.passkey_controls import (
    own_passkeys,
    remove_passkey,
    set_passkey_policy,
)
from apps.identity.passkey_forms import (
    PasskeyPolicyForm,
    PasskeyRegistrationForm,
    PasskeyRemovalForm,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


def passkey_continuation() -> str:
    """Resume every management challenge at the own account page."""
    return reverse("identity:passkeys")


def _page(
    request: HttpRequest, *, policy_form: PasskeyPolicyForm | None = None
) -> HttpResponseBase:
    user_id = current_actor_id()
    clinic = (
        Clinic.objects.filter(
            userclinicrole__user_id=user_id,
            userclinicrole__revoked_at__isnull=True,
        )
        .order_by("pk")
        .first()
    )
    if clinic is None:
        raise PasskeyDeniedError
    organization = Organization.objects.get(pk=clinic.organization_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT NOT EXISTS ("
            "SELECT 1 FROM clinic_app.identity_clinic AS clinic "
            "WHERE clinic.organization_id = %s "
            "AND NOT clinic_app.has_permission('staff.organization', clinic.id, NULL)"
            ")",
            [clinic.organization_id],
        )
        can_manage = cursor.fetchone() == (True,)
    return auth_response(
        render(
            request,
            "identity/passkeys.html",
            {
                "clinic": clinic,
                "passkeys": own_passkeys(),
                "registration_form": PasskeyRegistrationForm(),
                "policy_form": policy_form
                or PasskeyPolicyForm(
                    initial={
                        "clinic_id": clinic.pk,
                        "enabled": organization.passkey_step_up_enabled,
                    }
                ),
                "can_manage_policy": can_manage,
                "policy_enabled": organization.passkey_step_up_enabled,
            },
            status=400 if policy_form is not None and policy_form.errors else 200,
        )
    )


@privileged_totp_required(passkey_continuation)
@require_GET
def passkeys_view(request: HttpRequest) -> HttpResponseBase:
    """List own protected labels and native removal/policy forms."""
    return _page(request)


@sensitive_post_parameters()
@require_POST
def passkey_remove(request: HttpRequest) -> HttpResponseBase:
    """Remove only an own body-selected credential after real fresh verification."""
    form = PasskeyRemovalForm(request.POST)
    if not form.is_valid():
        return auth_response(render(request, "400.html", status=400))
    clinic_id: UUID = form.cleaned_data["clinic_id"]
    passkey_id: UUID = form.cleaned_data["passkey_id"]
    try:
        remove_passkey(
            clinic_id=clinic_id,
            passkey_id=passkey_id,
            session_key=request_session_key(request),
        )
    except PasskeyDeniedError:
        return auth_response(render(request, "403.html", status=403))
    return auth_redirect(request, passkey_continuation())


@sensitive_post_parameters()
@require_POST
def passkey_policy(request: HttpRequest) -> HttpResponseBase:
    """Apply one explicit fresh, organization-wide administrator decision."""
    form = PasskeyPolicyForm(request.POST)
    if not form.is_valid():
        return _page(request, policy_form=form)
    clinic_id: UUID = form.cleaned_data["clinic_id"]
    enabled: bool = form.cleaned_data["enabled"]
    try:
        set_passkey_policy(
            clinic_id=clinic_id,
            enabled=enabled,
            session_key=request_session_key(request),
        )
    except PasskeyDeniedError:
        return auth_response(render(request, "403.html", status=403))
    return auth_redirect(request, passkey_continuation())
