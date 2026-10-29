"""Exact-clinic staff invitations, notified reset and retained offboarding UI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.identity.account_views import request_session_key
from apps.identity.invitation_management import revoke_invitation
from apps.identity.invitations import invite_staff
from apps.identity.lifecycle_authority import (
    LifecycleDeniedError,
    require_staff_management,
)
from apps.identity.models import AuthenticatorReset, StaffInvitation
from apps.identity.offboarding import offboard_staff
from apps.identity.otp import (
    auth_redirect,
    auth_response,
    flow_redirect,
    privileged_totp_required,
)
from apps.identity.recovery import (
    execute_authenticator_reset,
    request_authenticator_reset,
)
from apps.identity.session_inventory import SessionInventoryDeniedError
from apps.identity.staff_directory import staff_directory
from apps.identity.staff_forms import (
    AuthenticatorResetExecutionForm,
    AuthenticatorResetForm,
    InvitationRevocationForm,
    OffboardingForm,
    StaffInvitationForm,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
@privileged_totp_required(
    lambda clinic_id: reverse("identity:staff", kwargs={"clinic_id": clinic_id})
)
def staff_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Invite current staff only after current human management authorization."""
    _actor_id, clinic = require_staff_management(clinic_id=clinic_id)
    form = StaffInvitationForm(request.POST if request.method == "POST" else None)
    invited = False
    if request.method == "POST" and form.is_valid():
        email: str = form.cleaned_data["email"]
        bundle: str = form.cleaned_data["bundle"]
        try:
            invite_staff(clinic_id=clinic_id, email=email, bundle=bundle)
        except (LifecycleDeniedError, ValidationError):
            form.add_error(
                None, _("We could not send this invitation. Check the email and role.")
            )
        else:
            invited = True
    return auth_response(
        render(
            request,
            "identity/staff.html",
            {
                "clinic_id": clinic_id,
                "clinic": clinic,
                "members": staff_directory(clinic_id=clinic_id),
                "invitations": StaffInvitation.objects.filter(
                    clinic_id=clinic_id
                ).order_by("-created_at"),
                "resets": AuthenticatorReset.objects.filter(
                    clinic_id=clinic_id
                ).order_by("-created_at"),
                "form": form,
                "invited": invited,
            },
            status=400 if form.errors else 200,
        )
    )


@require_POST
@privileged_totp_required(
    lambda clinic_id: reverse("identity:staff", kwargs={"clinic_id": clinic_id})
)
def reset_request_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Require persisted fresh TOTP and queue, but never counterfeit, delivery."""
    require_staff_management(clinic_id=clinic_id)
    form = AuthenticatorResetForm(request.POST)
    if not form.is_valid():
        return auth_response(render(request, "400.html", status=400))
    target = reverse("identity:staff", kwargs={"clinic_id": clinic_id})
    user_id: UUID = form.cleaned_data["user_id"]
    try:
        request_authenticator_reset(
            clinic_id=clinic_id,
            user_id=user_id,
            session_key=request_session_key(request),
        )
    except SessionInventoryDeniedError:
        return flow_redirect(request, "identity:step-up", target)
    return auth_redirect(request, target)


@require_POST
@privileged_totp_required(
    lambda clinic_id: reverse("identity:staff", kwargs={"clinic_id": clinic_id})
)
def reset_execute_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Show a clear delay refusal while leaving every authenticator intact."""
    require_staff_management(clinic_id=clinic_id)
    form = AuthenticatorResetExecutionForm(request.POST)
    if not form.is_valid():
        return auth_response(render(request, "400.html", status=400))
    target = reverse("identity:staff", kwargs={"clinic_id": clinic_id})
    reset_id: UUID = form.cleaned_data["reset_id"]
    try:
        execute_authenticator_reset(
            clinic_id=clinic_id,
            reset_id=reset_id,
            session_key=request_session_key(request),
        )
    except SessionInventoryDeniedError:
        return flow_redirect(request, "identity:step-up", target)
    except LifecycleDeniedError:
        return auth_response(
            render(
                request, "identity/reset_wait.html", {"return_href": target}, status=409
            )
        )
    return auth_redirect(request, target)


@require_POST
@privileged_totp_required(
    lambda clinic_id: reverse("identity:staff", kwargs={"clinic_id": clinic_id})
)
def offboarding_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Revoke in one tenant transaction; retain all records and refusal guards."""
    require_staff_management(clinic_id=clinic_id)
    form = OffboardingForm(request.POST)
    if not form.is_valid():
        return auth_response(render(request, "400.html", status=400))
    user_id: UUID = form.cleaned_data["user_id"]
    offboard_staff(clinic_id=clinic_id, user_id=user_id)
    return auth_redirect(
        request, reverse("identity:staff", kwargs={"clinic_id": clinic_id})
    )


@require_POST
@privileged_totp_required(
    lambda clinic_id: reverse("identity:staff", kwargs={"clinic_id": clinic_id})
)
def invitation_revoke_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Retire a private pending token through the actual scoped state machine."""
    require_staff_management(clinic_id=clinic_id)
    form = InvitationRevocationForm(request.POST)
    if not form.is_valid():
        return auth_response(render(request, "400.html", status=400))
    invitation_id: UUID = form.cleaned_data["invitation_id"]
    revoke_invitation(clinic_id=clinic_id, invitation_id=invitation_id)
    return auth_redirect(
        request, reverse("identity:staff", kwargs={"clinic_id": clinic_id})
    )
