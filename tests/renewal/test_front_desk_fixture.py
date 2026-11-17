"""The front-desk browser fixture provisions the authority its policy needs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import Clinic, User, UserClinicRole

from identity.permission_support import owner_context
from renewal.front_desk_fixture import seed

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_seed_publishes_the_policy_after_another_scenario_added_a_clinic(
    rbac_graph: RbacGraph,
) -> None:
    # The browser administrator administers clinics A and B, as seed_ai_admin does.
    actor = User.objects.create(username=f"synthetic-desk-admin-{uuid4().hex}")
    with owner_context(rbac_graph.organization_a):
        for clinic in (rbac_graph.clinic_a, rbac_graph.clinic_b):
            for role in ("clinic_admin", "org_admin"):
                UserClinicRole.objects.create(
                    organization_id=rbac_graph.organization_a,
                    clinic_id=clinic,
                    user=actor,
                    role=role,
                )
        # An earlier scenario adds a unit the administrator holds no role in.
        added = Clinic.objects.create(
            organization_id=rbac_graph.organization_a,
            name="Synthetic added unit",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
    receipt = seed(actor.pk, rbac_graph.clinic_a, rbac_graph.organization_a)
    assert receipt["synthetic"] is True
    with owner_context(rbac_graph.organization_a):
        assert UserClinicRole.objects.filter(
            user=actor, clinic=added, role="org_admin"
        ).exists()
