"""The encounter permission extends the landed workflow and membership authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import UserClinicRole
from django.utils import timezone

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_unscheduled_permission_preserves_selected_workflow_bundle(
    rbac_graph: RbacGraph,
) -> None:
    # Given a registered physician with the explicitly issued workflow successor.
    actor, enrollment = permission_actor(rbac_graph, "physician")
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(user_id=actor).update(workflow_bundle_version=2)

    # When the integrated resolver decides both domains' permissions.
    with permission_context(rbac_graph, actor):
        admitted = {
            permission: require_permission(
                permission,
                clinic_id=rbac_graph.clinic_a,
                patient_enrollment_id=enrollment,
            )
            for permission in (
                "encounter.open_unscheduled",
                "tasks.view",
                "tasks.complete",
            )
        }

    # Then the new clinical branch has not erased the issued workflow branch.
    assert set(admitted.values()) == {actor}


def test_retained_revoked_membership_refuses_unscheduled_permission(
    rbac_graph: RbacGraph,
) -> None:
    # Given retained membership history and an otherwise valid registration.
    actor, enrollment = permission_actor(rbac_graph, "physician")
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(user_id=actor).update(revoked_at=timezone.now())

    # When the new permission is requested with that actor's real database scope.
    with permission_context(rbac_graph, actor), pytest.raises(CurrentActorError):
        require_permission(
            "encounter.open_unscheduled",
            clinic_id=rbac_graph.clinic_a,
            patient_enrollment_id=enrollment,
        )

    # Then retention of the assignment cannot keep its authority alive.
    with owner_context(rbac_graph.organization_a):
        assert UserClinicRole.all_objects.filter(user_id=actor).exists()
