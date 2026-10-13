"""Current-clinic no-delete offboarding and trusted-handler rollback proof."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity import offboarding_registry
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.models import OnboardingProgress, User, UserClinicRole
from apps.identity.offboarding import OffboardingHandlerError, offboard_staff
from apps.scheduling.models import AvailabilityBlock
from apps.tenancy.db import tenant_context
from django.utils import timezone

from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _target(graph: RbacGraph) -> User:
    target = User.objects.create(username=f"synthetic-offboard-{uuid4().hex}")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user=target,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    return target


def test_offboarding_retains_user_and_membership_history(
    rbac_graph: RbacGraph,
) -> None:
    target = _target(rbac_graph)
    assert offboarding_registry.registered_offboarding_handlers() == ()
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        offboard_staff(clinic_id=rbac_graph.clinic_b, user_id=target.pk)
    target.refresh_from_db()
    assert not target.is_active
    with owner_context(rbac_graph.organization_a):
        retained = UserClinicRole.all_objects.get(user=target)
        assert retained.revoked_at is not None
        assert not UserClinicRole.objects.filter(user=target).exists()


def test_offboarding_foreign_scope_leaves_target_active(
    rbac_graph: RbacGraph,
) -> None:
    target = _target(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(LifecycleDeniedError),
    ):
        offboard_staff(clinic_id=rbac_graph.clinic_a, user_id=target.pk)
    target.refresh_from_db()
    assert target.is_active


def test_non_none_handler_cannot_commit_its_work_or_offboarding(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(rbac_graph)
    observed: list[tuple[UUID, UUID]] = []

    def wrong_handler(*, clinic_id: UUID, user_id: UUID) -> bool:
        observed.append((clinic_id, user_id))
        OnboardingProgress.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=clinic_id,
            step_key="rollback_probe",
        )
        return True

    monkeypatch.setattr(offboarding_registry, "_handlers", [wrong_handler])
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(OffboardingHandlerError),
    ):
        offboard_staff(clinic_id=rbac_graph.clinic_b, user_id=target.pk)
    assert observed == [(rbac_graph.clinic_b, target.pk)]
    target.refresh_from_db()
    assert target.is_active
    with owner_context(rbac_graph.organization_a):
        assert not OnboardingProgress.objects.filter(
            clinic_id=rbac_graph.clinic_b, step_key="rollback_probe"
        ).exists()


def test_final_owner_cannot_be_offboarded_by_organization_administrator(
    rbac_graph: RbacGraph,
) -> None:
    target = _target(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        for user_id, role in (
            (rbac_graph.clinic_admin, UserClinicRole.Role.ORG_ADMIN),
            (target.pk, UserClinicRole.Role.OWNER),
        ):
            UserClinicRole.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_b,
                user_id=user_id,
                role=role,
            )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(LifecycleDeniedError),
    ):
        offboard_staff(clinic_id=rbac_graph.clinic_b, user_id=target.pk)
    target.refresh_from_db()
    assert target.is_active
    with owner_context(rbac_graph.organization_a):
        assert UserClinicRole.objects.filter(
            user=target, role=UserClinicRole.Role.OWNER
        ).exists()


@pytest.mark.parametrize("transfer", [False, True])
def test_future_work_requires_completed_synchronous_handler(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, *, transfer: bool
) -> None:
    graph = rbac_graph
    target = _target(graph)
    start = (timezone.now() + timedelta(days=1)).replace(second=0, microsecond=0)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user=target,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        block = AvailabilityBlock.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            practitioner=target,
            start_at=start,
            end_at=start + timedelta(hours=1),
            idempotency_key=uuid4(),
            create_fingerprint=b"o" * 32,
        )

    def completed_transfer(*, clinic_id: UUID, user_id: UUID) -> None:
        assert (clinic_id, user_id) == (graph.clinic_b, target.pk)
        AvailabilityBlock.objects.filter(pk=block.pk).update(retired_at=timezone.now())

    if transfer:
        monkeypatch.setattr(offboarding_registry, "_handlers", [completed_transfer])
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        if transfer:
            offboard_staff(clinic_id=graph.clinic_b, user_id=target.pk)
        else:
            with pytest.raises(LifecycleDeniedError):
                offboard_staff(clinic_id=graph.clinic_b, user_id=target.pk)
    target.refresh_from_db()
    assert target.is_active is not transfer
    with owner_context(graph.organization_a):
        block.refresh_from_db()
        assert (block.retired_at is not None) is transfer
