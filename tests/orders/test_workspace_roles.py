"""Page eligibility never substitutes for a patient's clinical permissions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from django.urls import reverse

from core.test_navigation import _client_for, _get

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_result_workspace_enforces_role_capabilities(
    rbac_graph: RbacGraph, role: str
) -> None:
    # Given a real, verified staff actor with the released default bundle.
    client, _user = _client_for(rbac_graph, role)
    own = reverse("orders:results", kwargs={"clinic_id": rbac_graph.clinic_a})
    unknown = reverse("orders:results", kwargs={"clinic_id": uuid4()})

    # When the actual tenant middleware and result view authorize the page.
    response = _get(client, own)

    # Then the clinic page opens only for its declared demographic permission.
    # Patient-specific orders, values and actions keep their independent gates.
    if "demographics.read" in BUNDLES_V1[role]:
        assert response.status_code == 200
        assert b'data-module="inbox"' in response.content
    else:
        denied = _get(client, unknown)
        assert response.status_code in {403, 404}
        assert response.status_code == denied.status_code
