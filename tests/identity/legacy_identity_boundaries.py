"""Legacy ORM, request permission, and authentication decorator decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.comms.adapters import OperationScope
from apps.core.api.authentication import StepUpVerified
from apps.core.integration import _actor_has_clinic_authority
from apps.identity import current_context, otp, services, stepup
from apps.identity.models import UserClinicRole
from apps.identity.permissions import IsClinicAdminForClinic, IsPhysicianForClinic
from apps.retention import views as retention_views
from apps.scheduling import access
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from identity.legacy_parity_support import (
    ADMINS,
    LEGACY,
    MANAGERS,
    PHYSICIAN,
    Boundary,
    has_rows,
    http_allowed,
)

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld


class PhysicianClinics(services.ClinicRoleQuerySetMixin):
    required_roles = (UserClinicRole.Role.PHYSICIAN,)


def _drf(w: LegacyWorld, valid: bool, *, admin: bool) -> bool:
    request = Request(RequestFactory().get("/workspace/"))
    request.user = w.actor
    view = APIView()
    view.kwargs = {"clinic_id": w.clinic_for(valid)}
    permission = IsClinicAdminForClinic() if admin else IsPhysicianForClinic()
    return permission.has_permission(request, view)


def _ui_permission(w: LegacyWorld, valid: bool, *, verified: bool) -> None:
    request = Request(w.request)
    request.user = (
        (w.request.user if verified else w.actor) if valid else AnonymousUser()
    )
    view = APIView()
    # The shipped API composes authentication with the TOTP permission.
    # Unverified receptionists remain allowed; privileged roles must fail.
    view.permission_classes = [IsAuthenticated, StepUpVerified]
    view.check_permissions(request)


def _actor(w: LegacyWorld, valid: bool, *, username: bool = False) -> object:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)", [str(uuid4())]
            )
    return (
        current_context.current_actor_username()
        if username
        else current_context.current_actor_id()
    )


def _totp(w: LegacyWorld, valid: bool) -> object:
    # A plain endpoint is the real public contract of this decorator; authority
    # and confirmed devices remain backed by the real models/database.
    request = w.request
    original_user = request.user
    if not valid:
        request.user = AnonymousUser()
    decorated = otp.privileged_totp_required(lambda: "/workspace/")(
        lambda _request: HttpResponse(status=204)
    )
    try:
        return decorated(request)
    finally:
        request.user = original_user


def _step_up(w: LegacyWorld, valid: bool, *, decorator: bool) -> object:
    original = w.request.session.get(stepup.STEP_UP_SESSION_KEY)
    if not valid:
        w.request.session.pop(stepup.STEP_UP_SESSION_KEY, None)
    try:
        if decorator:
            guarded = stepup.require_recent_verification()(
                lambda _request: HttpResponse(status=204)
            )
            return guarded(w.request)
        stepup.assert_step_up(w.request)
        return None
    finally:
        w.request.session[stepup.STEP_UP_SESSION_KEY] = original


BOUNDARIES = (
    Boundary(
        "apps.core.api.authentication.StepUpVerified.has_permission",
        "drf",
        LEGACY,
        lambda w, ok: _ui_permission(w, ok, verified=True),
    ),
    Boundary(
        "apps.core.api.authentication.StepUpVerified.has_permission#unverified",
        "drf",
        ("receptionist",),
        lambda w, ok: _ui_permission(w, ok, verified=False),
    ),
    Boundary(
        "apps.scheduling.access.authorized_view_scope",
        "scope",
        LEGACY,
        lambda w, ok: access.authorized_view_scope(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.scheduling.access.authorized_manager_clinic",
        "scope",
        MANAGERS,
        lambda w, ok: access.authorized_manager_clinic(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.scheduling.access.authorized_appointment_manager_clinic",
        "scope",
        MANAGERS,
        lambda w, ok: access.authorized_appointment_manager_clinic(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.retention.views._is_manager",
        "membership",
        ADMINS,
        lambda w, ok: retention_views._is_manager(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.retention.views._is_physician",
        "membership",
        PHYSICIAN,
        lambda w, ok: retention_views._is_physician(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.core.integration._actor_has_clinic_authority",
        "membership",
        LEGACY,
        lambda w, ok: _actor_has_clinic_authority(
            OperationScope(
                operation_id=uuid4(),
                organization_id=w.graph.organization_a,
                clinic_id=w.clinic_for(ok),
                actor_id=w.actor.pk,
            )
        ),
    ),
    Boundary("apps.identity.current_context.current_actor_id", "actor", LEGACY, _actor),
    Boundary(
        "apps.identity.current_context.current_actor_username",
        "actor",
        LEGACY,
        lambda w, ok: _actor(w, ok, username=True),
    ),
    Boundary(
        "apps.identity.current_context.require_current_actor_org_admin",
        "organization",
        ADMINS,
        lambda w, ok: current_context.require_current_actor_org_admin(
            w.graph.organization_a if ok else w.graph.organization_b,
            (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN),
        ),
    ),
    Boundary(
        "apps.identity.current_context.require_current_actor_clinic_roles",
        "roles",
        LEGACY,
        lambda w, ok: current_context.require_current_actor_clinic_roles(
            w.clinic_for(ok), (UserClinicRole.Role(w.role),)
        ),
    ),
    Boundary(
        "apps.identity.current_context.list_active_clinic_physicians",
        "roles",
        MANAGERS,
        lambda w, ok: current_context.list_active_clinic_physicians(w.clinic_for(ok)),
        has_rows,
    ),
    Boundary(
        "apps.identity.current_context._active_clinic_physicians",
        "resolver",
        MANAGERS,
        lambda w, ok: current_context._active_clinic_physicians(w.clinic_for(ok)),
        has_rows,
    ),
    Boundary(
        "apps.identity.services.resolve_login_organization",
        "resolver",
        LEGACY,
        lambda w, ok: services.resolve_login_organization(
            services.UserId(w.actor.pk if ok else uuid4())
        ),
        has_rows,
    ),
    Boundary(
        "apps.identity.services.has_clinic_role",
        "roles",
        LEGACY,
        lambda w, ok: services.has_clinic_role(
            services.UserId(w.actor.pk),
            services.ClinicId(w.clinic_for(ok)),
            (UserClinicRole.Role(w.role),),
        ),
    ),
    Boundary(
        "apps.identity.services.role_assignments_for_user",
        "membership",
        LEGACY,
        lambda w, ok: services.role_assignments_for_user(
            services.UserId(w.actor.pk if ok else uuid4())
        ),
        has_rows,
    ),
    Boundary(
        "apps.identity.services.clinics_for_user_roles",
        "roles",
        PHYSICIAN,
        lambda w, ok: services.clinics_for_user_roles(
            services.UserId(w.actor.pk if ok else uuid4()),
            (UserClinicRole.Role.PHYSICIAN,),
        ),
        has_rows,
    ),
    Boundary(
        "apps.identity.services.ClinicRoleQuerySetMixin.clinics_for_user",
        "mixin",
        PHYSICIAN,
        lambda w, ok: PhysicianClinics().clinics_for_user(
            services.UserId(w.actor.pk if ok else uuid4())
        ),
        has_rows,
    ),
    Boundary(
        "apps.identity.permissions.IsPhysicianForClinic.has_permission",
        "drf",
        PHYSICIAN,
        lambda w, ok: _drf(w, ok, admin=False),
    ),
    Boundary(
        "apps.identity.permissions.IsClinicAdminForClinic.has_permission",
        "drf",
        ADMINS,
        lambda w, ok: _drf(w, ok, admin=True),
    ),
    Boundary(
        "apps.identity.otp.privileged_totp_required",
        "decorator",
        LEGACY,
        _totp,
        http_allowed,
    ),
    Boundary(
        "apps.identity.stepup._freshness_is_valid",
        "verification",
        LEGACY,
        lambda w, ok: _step_up(w, ok, decorator=False),
    ),
    Boundary(
        "apps.identity.stepup.assert_step_up",
        "verification",
        LEGACY,
        lambda w, ok: _step_up(w, ok, decorator=False),
    ),
    Boundary(
        "apps.identity.stepup.require_recent_verification",
        "decorator",
        LEGACY,
        lambda w, ok: _step_up(w, ok, decorator=True),
        http_allowed,
    ),
)
