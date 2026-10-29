"""Current account/session authority at the actual lifecycle boundaries."""

from __future__ import annotations

from copy import copy
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.comms.adapters import OperationScope
from apps.comms.models import IntegrationOperation
from apps.identity import (
    account_views,
    invitation_management,
    invitations,
    notifications,
    onboarding,
    onboarding_views,
    recovery,
    session_inventory,
    staff_views,
)
from apps.identity.lifecycle_authority import require_staff_management
from apps.identity.models import SessionRecord, UserClinicRole
from apps.identity.offboarding import offboard_staff
from apps.identity.staff_directory import staff_directory
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import RequestFactory
from django.utils import timezone

from auth.stepup_test_support import create_role_actor, logged_in_client
from auth.test_recovery_codes import _deliver
from identity.legacy_parity_support import (
    ADMINS,
    LEGACY,
    Boundary,
    has_rows,
    http_allowed,
)
from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from otp_test_support import create_totp_device, fixed_otp_time, runtime_role, token_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from django.http import HttpRequest, HttpResponseBase

    from identity.legacy_parity_support import LegacyWorld


def lifecycle_world(
    subject: LegacyWorld, reset_clock: Callable[[datetime], None]
) -> LegacyWorld:
    """Use actual HTTP verification, not session flags or a synthetic issuer."""
    target = create_role_actor(subject.graph, UserClinicRole.Role.RECEPTIONIST)
    target.email = uuid4().hex + "@example.invalid"
    target.save(update_fields=("email",))
    device = create_totp_device(subject.actor.pk, confirmed=True)
    client = logged_in_client(subject.actor.pk)
    with runtime_role(), fixed_otp_time():
        verified = client.post(
            "/auth/step-up/",
            {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
                "next": "/auth/protected/",
            },
        )
    assert verified.status_code == 302
    with runtime_role():
        response = client.get("/auth/protected/")
    assert response.status_code == 200
    manager = create_role_actor(subject.graph, UserClinicRole.Role.CLINIC_ADMIN)
    manager_client = verified_client(replace(subject.graph, clinic_admin=manager.pk))
    with (
        runtime_role(),
        tenant_context(manager.pk, subject.graph.organization_a),
    ):
        reset = recovery.request_authenticator_reset(
            clinic_id=subject.clinic,
            user_id=target.pk,
            session_key=session_key(manager_client),
        )
        invitation = invitations.invite_staff(
            clinic_id=subject.clinic,
            email=uuid4().hex + "@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
        operation_id = reset.notification_operation_id
        assert operation_id is not None
        operation = IntegrationOperation.objects.get(pk=operation_id)
    reset_clock(timezone.now())
    _deliver(reset)
    with owner_context(subject.graph.organization_a):
        reset.refresh_from_db()
    assert reset.available_at is not None
    reset_clock(reset.available_at)
    return replace(
        subject,
        request=response.wsgi_request,
        lifecycle_target=target.pk,
        lifecycle_reset=reset.pk,
        lifecycle_invitation=invitation.pk,
        lifecycle_operation=operation,
    )


def _target(subject: LegacyWorld) -> UUID:
    target = subject.lifecycle_target
    assert target is not None
    return target


def _key(subject: LegacyWorld) -> str:
    key = subject.request.session.session_key
    assert key is not None
    return key


def _reset(subject: LegacyWorld) -> UUID:
    reset = subject.lifecycle_reset
    assert reset is not None
    return reset


def _invitation(subject: LegacyWorld) -> UUID:
    invitation = subject.lifecycle_invitation
    assert invitation is not None
    return invitation


def _operation(subject: LegacyWorld) -> IntegrationOperation:
    operation = subject.lifecycle_operation
    assert operation is not None
    return operation


def _reset_eligible(subject: LegacyWorld, valid: bool) -> bool:
    operation = _operation(subject)
    return notifications.reset_send_eligible(
        OperationScope(
            operation_id=operation.pk,
            organization_id=operation.organization_id,
            clinic_id=subject.clinic_for(valid),
            actor_id=operation.actor_id,
        )
    )


def _email(subject: LegacyWorld, valid: bool) -> notifications.IdentityEmail:
    operation = copy(_operation(subject))
    operation.clinic_id = subject.clinic_for(valid)
    return notifications.IdentityEmailAdapter().prepare(operation)


def _verified(subject: LegacyWorld, valid: bool) -> None:
    device_id = _record(subject).verification_device_id
    assert device_id is not None
    session_inventory.record_verified_session(
        request=subject.request,
        device_id=device_id if valid else -1,
        old_key=None,
    )


def _cookie(subject: LegacyWorld, valid: bool) -> bool:
    assert session_inventory.revoke_session_hash(session_key=_key(subject))
    return session_inventory.session_cookie_is_revoked(
        session_key=_key(subject) if valid else uuid4().hex
    )


def _supersede(subject: LegacyWorld, valid: bool) -> bool:
    session_inventory.supersede_session_hash(
        session_key=_key(subject) if valid else uuid4().hex
    )
    record = _record(subject)
    return record.superseded_at is not None and record.revoked_at is not None


def _post(
    subject: LegacyWorld,
    valid: bool,
    view: Callable[[HttpRequest, UUID], HttpResponseBase],
    payload: dict[str, UUID | str],
) -> HttpResponseBase:
    request = copy(subject.request)
    request.method = "POST"
    request.POST = (
        RequestFactory()
        .post(request.path, {key: str(value) for key, value in payload.items()})
        .POST
    )
    return view(request, subject.clinic_for(valid))


def _record(subject: LegacyWorld) -> SessionRecord:
    return SessionRecord.objects.get(
        session_key_hash=session_inventory.session_hash(_key(subject))
    )


def _recover(subject: LegacyWorld, valid: bool) -> bool:
    issued = recovery.issue_recovery_codes(
        clinic_id=subject.clinic, session_key=_key(subject)
    )
    return recovery.recover_authenticator(
        clinic_id=subject.clinic_for(valid),
        code=issued.codes[0],
    )


def _sessions(subject: LegacyWorld, valid: bool) -> object:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)", [str(uuid4())]
            )
    return session_inventory.own_sessions()


def _invite(subject: LegacyWorld, valid: bool) -> object:
    nonce = uuid4().hex
    return invitations.invite_staff(
        clinic_id=subject.clinic_for(valid),
        email=nonce + "@example.invalid",
        bundle=UserClinicRole.Role.RECEPTIONIST,
    )


BOUNDARIES = (
    Boundary(
        "apps.identity.notifications.reset_send_eligible",
        "lifecycle",
        ADMINS,
        _reset_eligible,
    ),
    Boundary(
        "apps.identity.notifications.IdentityEmailAdapter.prepare",
        "lifecycle",
        ADMINS,
        _email,
    ),
    Boundary(
        "apps.identity.onboarding.onboarding_progress",
        "lifecycle",
        ADMINS,
        lambda w, ok: onboarding.onboarding_progress(clinic_id=w.clinic_for(ok)),
    ),
    Boundary(
        "apps.identity.onboarding_views.onboarding_view",
        "lifecycle",
        ADMINS,
        lambda w, ok: onboarding_views.onboarding_view(w.request, w.clinic_for(ok)),
        http_allowed,
    ),
    Boundary(
        "apps.identity.onboarding_views.provider_review",
        "lifecycle",
        ADMINS,
        lambda w, ok: _post(w, ok, onboarding_views.provider_review, {}),
        http_allowed,
    ),
    Boundary(
        "apps.identity.session_inventory.register_session",
        "lifecycle",
        LEGACY,
        lambda w, ok: session_inventory.register_session(
            user_id=w.actor.pk if ok else uuid4(),
            session_key=uuid4().hex,
            expires_at=timezone.now() + timedelta(hours=1),
            network="",
            family="other",
        ),
    ),
    Boundary(
        "apps.identity.session_inventory.record_verified_session",
        "lifecycle",
        LEGACY,
        _verified,
    ),
    Boundary(
        "apps.identity.session_inventory.session_cookie_is_revoked",
        "lifecycle",
        LEGACY,
        _cookie,
    ),
    Boundary(
        "apps.identity.session_inventory.supersede_session_hash",
        "lifecycle",
        LEGACY,
        _supersede,
    ),
    Boundary(
        "apps.identity.lifecycle_authority.require_staff_management",
        "lifecycle",
        ADMINS,
        lambda w, ok: require_staff_management(clinic_id=w.clinic_for(ok)),
    ),
    Boundary(
        "apps.identity.staff_directory.staff_directory",
        "lifecycle",
        ADMINS,
        lambda w, ok: staff_directory(clinic_id=w.clinic_for(ok)),
        has_rows,
    ),
    Boundary(
        "apps.identity.account_views.recovery_view",
        "lifecycle",
        LEGACY,
        lambda w, ok: account_views.recovery_view(w.request, w.clinic_for(ok)),
        http_allowed,
    ),
    Boundary(
        "apps.identity.recovery.issue_recovery_codes",
        "lifecycle",
        LEGACY,
        lambda w, ok: recovery.issue_recovery_codes(
            clinic_id=w.clinic_for(ok), session_key=_key(w)
        ),
    ),
    Boundary(
        "apps.identity.recovery.recover_authenticator",
        "lifecycle",
        LEGACY,
        _recover,
    ),
    Boundary(
        "apps.identity.session_inventory.own_sessions",
        "lifecycle",
        LEGACY,
        _sessions,
        has_rows,
    ),
    Boundary(
        "apps.identity.session_inventory.require_verified_session",
        "lifecycle",
        LEGACY,
        lambda w, ok: session_inventory.require_verified_session(
            session_key=_key(w) if ok else uuid4().hex
        ),
    ),
    Boundary(
        "apps.identity.session_inventory.require_session_step_up",
        "lifecycle",
        ADMINS,
        lambda w, ok: session_inventory.require_session_step_up(
            clinic_id=w.clinic_for(ok), session_key=_key(w)
        ),
    ),
    Boundary(
        "apps.identity.session_inventory.session_is_active",
        "lifecycle",
        LEGACY,
        lambda w, ok: session_inventory.session_is_active(
            user_id=w.actor.pk if ok else uuid4(), session_key=_key(w)
        ),
    ),
    Boundary(
        "apps.identity.session_inventory.revoke_session",
        "lifecycle",
        LEGACY,
        lambda w, ok: session_inventory.revoke_session(
            session_record_id=_record(w).pk if ok else uuid4()
        ),
    ),
    Boundary(
        "apps.identity.session_inventory.revoke_session_hash",
        "lifecycle",
        LEGACY,
        lambda w, ok: session_inventory.revoke_session_hash(
            session_key=_key(w) if ok else uuid4().hex
        ),
    ),
    Boundary(
        "apps.identity.invitations.invite_staff",
        "lifecycle",
        ADMINS,
        _invite,
    ),
    Boundary(
        "apps.identity.offboarding.offboard_staff",
        "lifecycle",
        ADMINS,
        lambda w, ok: offboard_staff(clinic_id=w.clinic_for(ok), user_id=_target(w)),
    ),
    Boundary(
        "apps.identity.recovery.request_authenticator_reset",
        "lifecycle",
        ADMINS,
        lambda w, ok: recovery.request_authenticator_reset(
            clinic_id=w.clinic_for(ok),
            user_id=_target(w),
            session_key=_key(w),
        ),
    ),
    Boundary(
        "apps.identity.staff_views.staff_view",
        "lifecycle",
        ADMINS,
        lambda w, ok: staff_views.staff_view(w.request, w.clinic_for(ok)),
        http_allowed,
    ),
    Boundary(
        "apps.identity.recovery.execute_authenticator_reset",
        "lifecycle",
        ADMINS,
        lambda w, ok: recovery.execute_authenticator_reset(
            clinic_id=w.clinic_for(ok),
            reset_id=_reset(w),
            session_key=_key(w),
        ),
    ),
    Boundary(
        "apps.identity.staff_views.reset_request_view",
        "lifecycle",
        ADMINS,
        lambda w, ok: _post(
            w, ok, staff_views.reset_request_view, {"user_id": _target(w)}
        ),
        http_allowed,
    ),
    Boundary(
        "apps.identity.staff_views.reset_execute_view",
        "lifecycle",
        ADMINS,
        lambda w, ok: _post(
            w, ok, staff_views.reset_execute_view, {"reset_id": _reset(w)}
        ),
        http_allowed,
    ),
    Boundary(
        "apps.identity.staff_views.offboarding_view",
        "lifecycle",
        ADMINS,
        lambda w, ok: _post(
            w,
            ok,
            staff_views.offboarding_view,
            {"user_id": _target(w), "confirm": "on"},
        ),
        http_allowed,
    ),
    Boundary(
        "apps.identity.invitation_management.expire_pending_invitations",
        "lifecycle",
        ADMINS,
        lambda w, ok: invitation_management.expire_pending_invitations(
            clinic_id=w.clinic_for(ok)
        ),
    ),
    Boundary(
        "apps.identity.invitation_management.revoke_invitation",
        "lifecycle",
        ADMINS,
        lambda w, ok: invitation_management.revoke_invitation(
            clinic_id=w.clinic_for(ok), invitation_id=_invitation(w)
        ),
    ),
    Boundary(
        "apps.identity.staff_views.invitation_revoke_view",
        "lifecycle",
        ADMINS,
        lambda w, ok: _post(
            w,
            ok,
            staff_views.invitation_revoke_view,
            {"invitation_id": _invitation(w)},
        ),
        http_allowed,
    ),
)
