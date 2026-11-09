"""Organization governance extends the native settings surface, not a new SPA."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.identity.account_views import request_session_key
from apps.identity.enterprise import (
    approve_group_mapping,
    configure_connection,
    issue_scim_token,
)
from apps.identity.enterprise_access import review_break_glass, revoke_break_glass
from apps.identity.enterprise_authority import GovernanceDeniedError, admin_scope
from apps.identity.enterprise_forms import (
    ConnectionCommandForm,
    GrantCommandForm,
    GroupMappingForm,
    SsoConfigurationForm,
    SupportApprovalForm,
    TemplatePropagationForm,
    UnitAccessForm,
)
from apps.identity.enterprise_support_access import (
    approve_support_access,
    revoke_support_access,
)
from apps.identity.enterprise_units import (
    propagate_template,
    revoke_unit_access,
    rotate_scim_credentials,
)
from apps.identity.models import (
    BreakGlassGrant,
    Clinic,
    ScimResource,
    ScimRoleMapping,
    SsoConnection,
    SupportAccessGrant,
)
from apps.identity.otp import auth_response, privileged_totp_required
from apps.identity.staff_directory import staff_directory

if TYPE_CHECKING:
    from uuid import UUID

    from django import forms
    from django.http import HttpRequest, HttpResponseBase

FORM_TYPES = {
    "configure": (SsoConfigurationForm, _("Configure synthetic SSO")),
    "mapping": (GroupMappingForm, _("Approve group mapping")),
    "token": (ConnectionCommandForm, _("Issue SCIM credential")),
    "rotate": (ConnectionCommandForm, _("Rotate SCIM credential")),
    "support": (SupportApprovalForm, _("Approve masked support access")),
    "template": (TemplatePropagationForm, _("Propagate a template version")),
    "unit": (UnitAccessForm, _("Revoke unit access")),
}


def _apply(*, action: str, form: forms.Form, clinic_id: UUID, session_key: str) -> str:
    values = form.cleaned_data
    match action:
        case "configure":
            configure_connection(
                clinic_id=clinic_id,
                issuer=values["issuer"],
                client_id=values["client_id"],
                session_key=session_key,
            )
        case "mapping":
            approve_group_mapping(
                clinic_id=clinic_id,
                group_id=values["group_id"],
                unit_id=values["unit_id"],
                bundle=values["bundle"],
                session_key=session_key,
            )
        case "token":
            return issue_scim_token(
                clinic_id=clinic_id,
                connection_id=values["connection_id"],
                session_key=session_key,
            )
        case "rotate":
            return rotate_scim_credentials(
                clinic_id=clinic_id,
                connection_id=values["connection_id"],
                session_key=session_key,
            )
        case "support":
            approve_support_access(
                clinic_id=clinic_id,
                support_user_id=values["support_user_id"],
                purpose=values["purpose"],
                expires_at=timezone.now() + timedelta(minutes=values["minutes"]),
                session_key=session_key,
            )
        case "template":
            propagate_template(
                clinic_id=clinic_id,
                source_id=values["source_id"],
                unit_id=values["unit_id"],
                command_id=values["command_id"],
                session_key=session_key,
            )
        case "unit":
            revoke_unit_access(
                clinic_id=values["unit_id"],
                user_id=values["user_id"],
                command_id=values["command_id"],
                session_key=session_key,
            )
        case _:
            raise GovernanceDeniedError
    return ""


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
@privileged_totp_required(
    lambda clinic_id: reverse("identity:governance", args=(clinic_id,))
)
def governance_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Every action rechecks the persisted factor and organization-wide authority."""
    key = request_session_key(request)
    action = request.POST.get("action", "") if request.method == "POST" else ""
    if request.method == "POST" and action not in FORM_TYPES:
        return auth_response(render(request, "400.html", status=400))
    operations = []
    status = 200
    succeeded, secret = False, ""
    with (
        transaction.atomic(),
        admin_scope(clinic_id=clinic_id, session_key=key) as (_actor, clinic),
    ):
        for name, (form_type, title) in FORM_TYPES.items():
            form = form_type(request.POST if action == name else None, prefix=name)
            if action == name and form.is_valid():
                try:
                    secret = _apply(
                        action=action, form=form, clinic_id=clinic_id, session_key=key
                    )
                except (GovernanceDeniedError, ValidationError):
                    form.add_error(
                        None,
                        _(
                            "Access refused. Check the scope and confirm "
                            "your authenticator again."
                        ),
                    )
                else:
                    succeeded = True
            operations.append({"key": name, "title": title, "form": form})
            if form.errors:
                status = 400
        units = [
            {"clinic": unit, "members": staff_directory(clinic_id=unit.pk)}
            for unit in Clinic.objects.filter(
                organization_id=clinic.organization_id
            ).order_by("id")
        ]
        context = {
            "clinic": clinic,
            "clinic_id": clinic_id,
            "review_now": timezone.now(),
            "operations": operations,
            "succeeded": succeeded,
            "secret": secret,
            "connections": tuple(
                SsoConnection.objects.filter(clinic_id=clinic_id).order_by(
                    "-created_at"
                )
            ),
            "groups": tuple(
                ScimResource.objects.filter(
                    clinic_id=clinic_id, kind="Groups", deleted_at__isnull=True
                )
            ),
            "mappings": tuple(
                ScimRoleMapping.objects.filter(clinic_id=clinic_id).select_related(
                    "unit"
                )
            ),
            "emergencies": tuple(
                BreakGlassGrant.objects.filter(clinic_id=clinic_id).order_by(
                    "-created_at"
                )[:100]
            ),
            "support_grants": tuple(
                SupportAccessGrant.objects.filter(clinic_id=clinic_id).order_by(
                    "-created_at"
                )[:100]
            ),
            "units": units,
        }
        return auth_response(
            render(request, "identity/governance.html", context, status=status)
        )


@require_POST
@privileged_totp_required(
    lambda clinic_id: reverse("identity:emergency-reviews", args=(clinic_id,))
)
def grant_command(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """End or review an existing grant; the record selector remains in the body."""
    form = GrantCommandForm(request.POST)
    if not form.is_valid():
        return auth_response(render(request, "400.html", status=400))
    commands = {
        "break_glass_revoke": revoke_break_glass,
        "break_glass_review": review_break_glass,
        "support_revoke": revoke_support_access,
    }
    action = request.POST.get("action", "")
    command = commands.get(action)
    if command is None:
        return auth_response(render(request, "400.html", status=400))
    command(
        clinic_id=clinic_id,
        grant_id=form.cleaned_data["grant_id"],
        session_key=request_session_key(request),
    )
    return auth_response(
        render(
            request,
            "identity/governance_done.html",
            {
                "clinic_id": clinic_id,
                "emergency_command": action
                in {"break_glass_revoke", "break_glass_review"},
            },
        )
    )
