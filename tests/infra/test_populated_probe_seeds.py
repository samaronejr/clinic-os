"""Minimal archival fixtures for rows not retained by a current service."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.availability_publication import create_closure
from apps.scheduling.resource_definitions import ClosureInput
from apps.scheduling.resource_models import Absence, Holiday
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_legacy_static_token_can_be_seeded_by_owner(rbac_graph: RbacGraph) -> None:
    # New recovery enrollment deliberately never retains a legacy token.
    # Seed the older retained format without enrolling it for authentication.
    with owner_context(rbac_graph.organization_a):
        device = StaticDevice.objects.create(
            user_id=rbac_graph.physician, name="synthetic-f2", confirmed=False
        )
        row = StaticToken.objects.create(device=device, token=uuid4().hex[:8])
        assert StaticToken.objects.filter(pk=row.pk).exists()


def test_scheduler_can_publish_clinic_and_practitioner_closures(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.shared_user,
            role=UserClinicRole.Role.SCHEDULER,
        )
    with permission_context(graph, graph.shared_user):
        holiday = create_closure(
            clinic_id=graph.clinic_a,
            content=ClosureInput(
                start_local="2035-06-04T09:00",
                end_local="2035-06-04T12:00",
                reason=Holiday.Reason.HOLIDAY,
            ),
        )
        absence = create_closure(
            clinic_id=graph.clinic_a,
            content=ClosureInput(
                start_local="2035-06-05T09:00",
                end_local="2035-06-05T12:00",
                reason=Absence.Reason.UNAVAILABLE,
                practitioner_id=graph.physician,
            ),
        )
        assert isinstance(holiday, Holiday)
        assert isinstance(absence, Absence)
