"""Explicit trusted-owner opt-in for synthetic workflow actors only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.identity.models import UserClinicRole

from identity.permission_support import (
    owner_context,
)
from identity.permission_support import (
    permission_actor as identity_permission_actor,
)

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph


def permission_actor(graph: RbacGraph, role: str) -> tuple[UUID, UUID]:
    actor, enrollment = identity_permission_actor(graph, role)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=actor,
        ).update(workflow_bundle_version=2)
    return actor, enrollment
