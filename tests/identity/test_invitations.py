"""Real-role protected invitation/password/confirmed-TOTP regression proof."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity import invitation_views
from apps.identity.invitations import (
    enroll_invitation,
    invitation_token,
    invite_staff,
)
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.models import SessionRecord, StaffInvitation, User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import Client

from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time, get_totp_device, runtime_role, token_for

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_invitation_resumes_password_and_activates_only_after_confirmed_totp(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        invitation = invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-invited@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
        repeated = invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-invited@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
    assert invitation.pk == repeated.pk
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT email, token_hash FROM clinic_app.identity_staffinvitation "
            "WHERE id=%s",
            [invitation.pk],
        )
        stored = cursor.fetchone()
    assert stored is not None
    assert isinstance(stored[0], (bytes, memoryview))
    assert len(stored[1]) == 32
    assert not User.objects.get(pk=invitation.pending_user_id).is_active
    password = secrets.token_urlsafe(24) + "1!Aa"
    username = f"synthetic-invited-{uuid4().hex}"
    token = invitation_token(invitation)
    with runtime_role():
        first = enroll_invitation(token=token, username=username, password=password)
        resumed = enroll_invitation(token=token, username=username, password=password)
    assert first.device.pk == resumed.device.pk
    assert first.accepted_user_id is None
    assert not User.objects.get(pk=invitation.pending_user_id).is_active
    with owner_context(graph.organization_a):
        assert not UserClinicRole.objects.filter(
            user_id=invitation.pending_user_id
        ).exists()
    with runtime_role(), fixed_otp_time():
        completed = enroll_invitation(
            token=token,
            username=username,
            password=password,
            otp_token=token_for(
                get_totp_device(invitation.pending_user_id, confirmed=False)
            ),
        )
    assert completed.accepted_user_id == invitation.pending_user_id
    assert User.objects.get(pk=invitation.pending_user_id).is_active
    with owner_context(graph.organization_a):
        accepted = StaffInvitation.objects.get(pk=invitation.pk)
        assert accepted.state == StaffInvitation.State.ACCEPTED
        assert accepted.accepted_at is not None
        assert accepted.password_set_at is not None
        assert UserClinicRole.objects.filter(
            user_id=invitation.pending_user_id,
            clinic_id=graph.clinic_b,
            role=UserClinicRole.Role.RECEPTIONIST,
        ).exists()
        assert (
            UserClinicRole.objects.get(
                user_id=invitation.pending_user_id, clinic_id=graph.clinic_b
            ).workflow_bundle_version
            == 1
        )
    with runtime_role(), pytest.raises(LifecycleDeniedError):
        enroll_invitation(token=token, username=username, password=password)


def test_invitation_refuses_foreign_clinic_and_changed_resume_password(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        with pytest.raises(LifecycleDeniedError):
            invite_staff(
                clinic_id=graph.clinic_a,
                email="synthetic-foreign@example.invalid",
                bundle=UserClinicRole.Role.RECEPTIONIST,
            )
        invitation = invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-resume@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
    password = secrets.token_urlsafe(24) + "1!Aa"
    username = f"synthetic-resume-{uuid4().hex}"
    token = invitation_token(invitation)
    with runtime_role():
        staged = enroll_invitation(token=token, username=username, password=password)
        with pytest.raises(LifecycleDeniedError):
            enroll_invitation(
                token=token,
                username=username,
                password=secrets.token_urlsafe(24) + "2!Bb",
            )
        resumed = enroll_invitation(token=token, username=username, password=password)
    assert staged.device.pk == resumed.device.pk
    assert not resumed.device.confirmed
    assert not User.objects.get(pk=invitation.pending_user_id).is_active


def test_native_invitation_confirmation_logs_in_the_accepted_user(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        invitation = invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-http-invited@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
    password = secrets.token_urlsafe(24) + "1!Aa"
    payload = {
        "invitation_code": invitation_token(invitation),
        "username": f"synthetic-http-invited-{uuid4().hex}",
        "password": password,
        "password_confirmation": password,
    }
    client = Client()
    with runtime_role():
        staged = client.post("/auth/invitation/", payload)
    assert staged.status_code == 200
    assert "_auth_user_id" not in client.session
    with runtime_role(), fixed_otp_time():
        device = get_totp_device(invitation.pending_user_id, confirmed=False)
        accepted = client.post(
            "/auth/invitation/", {**payload, "otp_token": token_for(device)}
        )
    assert accepted.status_code == 302
    assert client.session["_auth_user_id"] == str(invitation.pending_user_id)
    assert client.session["active_org_id"] == str(graph.organization_a)
    with runtime_role():
        response = client.get("/account/sessions/")
    assert response.status_code == 200
    assert b"data-session-record" in response.content


def test_failed_confirmation_response_rolls_back_activation_and_new_session(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        invitation = invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-http-rollback@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
    password = secrets.token_urlsafe(24) + "1!Aa"
    payload = {
        "invitation_code": invitation_token(invitation),
        "username": f"synthetic-http-rollback-{uuid4().hex}",
        "password": password,
        "password_confirmation": password,
    }
    client = Client()
    with runtime_role():
        assert client.post("/auth/invitation/", payload).status_code == 200

    def failed_response(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(invitation_views, "auth_redirect", failed_response)
    with runtime_role(), fixed_otp_time():
        device = get_totp_device(invitation.pending_user_id, confirmed=False)
        with pytest.raises(RuntimeError):
            client.post(
                "/auth/invitation/", {**payload, "otp_token": token_for(device)}
            )
    assert not User.objects.get(pk=invitation.pending_user_id).is_active
    with owner_context(graph.organization_a):
        invitation.refresh_from_db()
        assert invitation.state == StaffInvitation.State.PENDING
        assert not SessionRecord.objects.filter(
            user_id=invitation.pending_user_id
        ).exists()
