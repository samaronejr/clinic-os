"""Worker-local duplicate refusals credit the parallel refusal-exit guard."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.intake import services
from django.urls import reverse

from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context
from patient_http_support import audit_event_types
from patient_service_support import runtime_role
from patients.duplicate_support import _pair

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("action", ["review_decision", "unmerge"])
def test_duplicate_decision_without_selector_is_a_silent_refusal(
    rbac_graph: RbacGraph, action: str
) -> None:
    # Given: real clinic-review authority and an exactly verified HTTP session.
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    client = verified_client(rbac_graph)
    url = reverse("intake:patient-duplicates", args=(rbac_graph.clinic_a,))
    before = audit_event_types(rbac_graph, rbac_graph.clinic_admin)

    # When: a valid decision action omits its optional form UUID.
    with runtime_role():
        refused = client.post(
            url, {"action": action, "reason": "Synthetic refusal witness"}
        )

    # Then: the actual view exit refuses without audit or session mutation.
    assert refused.status_code == 404
    assert "sessionid" not in refused.cookies
    assert not refused.wsgi_request.session.modified
    assert audit_event_types(rbac_graph, rbac_graph.clinic_admin) == before


@pytest.mark.parametrize(
    "case",
    [
        "malformed candidate",
        "unknown candidate",
        "unsupported candidate action",
        "malformed timeline selector",
        "unknown timeline enrollment",
    ],
)
def test_duplicate_and_timeline_selectors_refuse_without_side_effects(
    rbac_graph: RbacGraph, case: str
) -> None:
    # Given: only ordinary rows on this worker's active database connection.
    _first, _second, candidate = _pair(rbac_graph, services)
    client = verified_client(rbac_graph)
    duplicates = reverse("intake:patient-duplicates", args=(rbac_graph.clinic_a,))
    timeline = reverse("intake:patient-timeline", args=(rbac_graph.clinic_a,))
    requests = {
        "malformed candidate": (
            duplicates,
            {"action": "review", "candidate_id": "not-a-uuid"},
        ),
        "unknown candidate": (
            duplicates,
            {"action": "review", "candidate_id": str(uuid4())},
        ),
        "unsupported candidate action": (
            duplicates,
            {"action": "synthetic-invalid", "candidate_id": str(candidate.pk)},
        ),
        "malformed timeline selector": (
            timeline,
            {"enrollment_id": "not-a-uuid"},
        ),
        "unknown timeline enrollment": (
            timeline,
            {"enrollment_id": str(uuid4())},
        ),
    }
    url, body = requests[case]
    before = audit_event_types(rbac_graph, rbac_graph.clinic_admin)

    # When: the real POST surface receives the invalid or unknown selector.
    with runtime_role():
        refused = client.post(url, body)

    # Then: the existing refusal exit has no audit or session side effect.
    assert refused.status_code == 404
    assert "sessionid" not in refused.cookies
    assert not refused.wsgi_request.session.modified
    assert audit_event_types(rbac_graph, rbac_graph.clinic_admin) == before
