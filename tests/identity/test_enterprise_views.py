"""Native enterprise forms keep selectors in bodies and responses private."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.test import override_settings

from identity.enterprise_support import enterprise_setup
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_governance_page_uses_native_private_forms(rbac_graph: RbacGraph) -> None:
    # Given a current organization-wide admin with real recent verification.
    setup = enterprise_setup(rbac_graph)
    # When the unit-governance page is requested as the runtime role.
    with runtime_role():
        response = setup.client.get(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/"
        )
    # Then no JavaScript or query selector is needed to perform its actions.
    assert response.status_code == 200
    assert b"data-enterprise-governance" in response.content
    assert b'method="post"' in response.content
    assert b'name="csrfmiddlewaretoken"' in response.content
    assert "no-store" in response.headers["Cache-Control"]
    assert setup.token.encode("ascii") not in response.content
