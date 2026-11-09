"""Unit governance keeps canonical membership and template versions authoritative."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.models import SpecialtyTemplate
from apps.ehr.services import publish_template
from apps.identity.enterprise_units import propagate_template, revoke_unit_access
from apps.identity.models import UnitGovernanceChange, UserClinicRole
from django.test import override_settings

from ehr.test_templates_v2 import definition
from identity.enterprise_support import enterprise_setup
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from renewal.test_encounters import seed

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_unit_revocation_is_idempotent_and_preserves_other_unit_memberships(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    user, _enrollment = permission_actor(rbac_graph, "receptionist")
    with owner_context(rbac_graph.organization_a):
        other = UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=user,
            role="receptionist",
        )
    command = uuid4()
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        for _ in range(2):
            revoke_unit_access(
                clinic_id=rbac_graph.clinic_a,
                user_id=user,
                command_id=command,
                session_key=setup.session_key,
            )
        assert UnitGovernanceChange.objects.filter(pk=command).count() == 1
    with owner_context(rbac_graph.organization_a):
        assert not UserClinicRole.objects.filter(
            user_id=user, clinic_id=rbac_graph.clinic_a, revoked_at__isnull=True
        ).exists()
        other.refresh_from_db()
        assert other.revoked_at is None


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_template_propagation_appends_a_version_and_replays_the_same_receipt(
    rbac_graph: RbacGraph,
) -> None:
    seed(rbac_graph)
    setup = enterprise_setup(rbac_graph)
    with permission_context(rbac_graph, rbac_graph.physician):
        source = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="synthetic_governance",
            title="Synthetic governance",
            prompts=definition(),
        )
    command = uuid4()
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        first = propagate_template(
            clinic_id=rbac_graph.clinic_a,
            source_id=source.pk,
            unit_id=rbac_graph.clinic_b,
            command_id=command,
            session_key=setup.session_key,
        )
        replay = propagate_template(
            clinic_id=rbac_graph.clinic_a,
            source_id=source.pk,
            unit_id=rbac_graph.clinic_b,
            command_id=command,
            session_key=setup.session_key,
        )
    assert first == replay
    with owner_context(rbac_graph.organization_a):
        propagated = SpecialtyTemplate.objects.get(pk=first)
        source.refresh_from_db()
        assert propagated.clinic_id == rbac_graph.clinic_b
        assert propagated.prompts == source.prompts
        assert propagated.pk != source.pk
        assert propagated.version == 1
