"""Cancellation cannot strand an organization budget reservation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.models import AIInvocation
from apps.ai.tasks import run_operation
from apps.identity.models import User, UserClinicRole
from django.utils import timezone

from ai.support import enqueue, seed
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("inactive_actor", [False, True])
def test_revoked_actor_cancellation_releases_the_unspent_reservation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inactive_actor: bool,
) -> None:
    # Given: a committed reservation followed by committed role revocation.
    world = seed(rbac_graph, monkeypatch)
    result = enqueue(world)
    assert result.operation_id is not None
    assert result.invocation_id is not None
    with owner_context(rbac_graph.organization_a):
        if inactive_actor:
            assert (
                User.objects.filter(pk=rbac_graph.shared_user).update(is_active=False)
                == 1
            )
        else:
            UserClinicRole.all_objects.filter(
                clinic_id=rbac_graph.clinic_a, user_id=rbac_graph.shared_user
            ).update(revoked_at=timezone.now())
    monkeypatch.setattr("apps.ai.tasks.fair_acquire", lambda **_kwargs: True)
    # When: the real worker reaches core's pre-adapter actor cancellation.
    with runtime_role():
        assert run_operation(operation_id=str(result.operation_id)) == "cancelled"
    # Then: metadata is settled without decrypting or calling a provider.
    with owner_context(rbac_graph.organization_a):
        invocation = AIInvocation.objects.get(pk=result.invocation_id)
        assert invocation.status == "failed"
        assert invocation.reason_code == "authority_revoked"
        assert invocation.cost_micros == 0
