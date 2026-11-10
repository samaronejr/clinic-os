"""Native enterprise forms keep selectors in bodies and responses private."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.test import override_settings

from core.test_navigation import _client_for
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
    # And a refused action is the view's own 400 form error: an unknown
    # connection yields no credential.
    with runtime_role():
        refused = setup.client.post(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/",
            {"action": "token", "token-connection_id": str(uuid4())},
        )
    assert refused.status_code == 400
    assert refused.context["succeeded"] is False
    assert refused.context["secret"] == ""
    # And an action outside the closed governance forms is the view's own 400.
    with runtime_role():
        unknown = setup.client.post(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/",
            {"action": "unknown"},
        )
    assert unknown.status_code == 400


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_unit_scoped_administrator_sees_why_governance_is_unavailable(
    rbac_graph: RbacGraph,
) -> None:
    # Given an owner whose organization authority covers only one of its units.
    client, _owner = _client_for(rbac_graph, "owner")
    url = f"/clinics/{rbac_graph.clinic_a}/settings/governance/"
    # When the page navigation offers is opened and an action is attempted.
    with runtime_role():
        page = client.get(url)
        action = client.post(
            url, {"action": "token", "token-connection_id": str(uuid4())}
        )
    # Then the page explains the missing authority without organization data,
    # and the action is still refused.
    assert page.status_code == 200
    assert b"data-governance-unavailable" in page.content
    assert b"data-enterprise-governance" not in page.content
    assert action.status_code == 403
