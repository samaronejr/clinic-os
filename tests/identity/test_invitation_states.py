"""Expired/revoked token refusal, retained history and genuine reinvitation."""

from __future__ import annotations

import secrets
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity import invitations
from apps.identity.invitation_management import revoke_invitation
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.models import StaffInvitation, UserClinicRole
from apps.tenancy.db import tenant_context
from django.utils import timezone

from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_expired_invitation_is_refused_and_reinvited_with_retained_history(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    earlier = timezone.now() - timedelta(days=3)
    with monkeypatch.context() as clock:
        clock.setattr(invitations, "timezone", SimpleNamespace(now=lambda: earlier))
        clock.setattr(
            StaffInvitation._meta.get_field("created_at"),
            "pre_save",
            lambda _instance, add: earlier,
        )
        with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
            old = invitations.invite_staff(
                clinic_id=graph.clinic_b,
                email="synthetic-expired@example.invalid",
                bundle=UserClinicRole.Role.RECEPTIONIST,
            )
    token = invitations.invitation_token(old)
    with runtime_role(), pytest.raises(LifecycleDeniedError):
        invitations.enroll_invitation(
            token=token,
            username=f"synthetic-expired-{uuid4().hex}",
            password=secrets.token_urlsafe(24) + "1!Aa",
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        replacement = invitations.invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-expired@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
    assert replacement.pk != old.pk
    assert replacement.pending_user_id != old.pending_user_id
    with owner_context(graph.organization_a):
        old.refresh_from_db()
        assert old.state == StaffInvitation.State.EXPIRED
        assert replacement.state == StaffInvitation.State.PENDING
        assert (
            StaffInvitation.objects.filter(pk__in=(old.pk, replacement.pk)).count() == 2
        )


def test_revoked_invitation_cannot_enroll_and_foreign_revoke_has_no_effect(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        invitation = invitations.invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-revoked@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
        with pytest.raises(LifecycleDeniedError):
            revoke_invitation(clinic_id=graph.clinic_a, invitation_id=invitation.pk)
        revoke_invitation(clinic_id=graph.clinic_b, invitation_id=invitation.pk)
        revoke_invitation(clinic_id=graph.clinic_b, invitation_id=invitation.pk)
    with runtime_role(), pytest.raises(LifecycleDeniedError):
        invitations.enroll_invitation(
            token=invitations.invitation_token(invitation),
            username=f"synthetic-revoked-{uuid4().hex}",
            password=secrets.token_urlsafe(24) + "1!Aa",
        )
    with owner_context(graph.organization_a):
        invitation.refresh_from_db()
        assert invitation.state == StaffInvitation.State.REVOKED
        assert invitation.revoked_at is not None
