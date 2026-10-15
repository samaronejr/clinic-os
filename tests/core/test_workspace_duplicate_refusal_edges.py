"""Execute every duplicate-selection and aliased-booking refusal over HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.intake import services
from django.urls import reverse

from identity.lifecycle_test_support import verified_client
from patient_service_support import runtime_role
from patients.duplicate_support import _merged_pair, _pair
from patients.test_patient_cpf_error_contract import _physical_rows, _refused_request
from scheduling.appointment_http_support import create_payload
from scheduling.appointment_service_support import seed_appointment_setup

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "body",
    [
        {"action": "review", "candidate_id": "not-a-uuid"},
        {"action": "unmerge"},
        {"action": "unknown"},
    ],
)
def test_duplicate_selection_refuses_without_state_changes(
    rbac_graph: RbacGraph, body: dict[str, str]
) -> None:
    _first, _second, candidate = _pair(rbac_graph, services)
    client = verified_client(rbac_graph)
    url = reverse("intake:patient-duplicates", args=(rbac_graph.clinic_a,))
    data = {"candidate_id": str(candidate.pk), **body}

    response = _refused_request(client, url, data)

    assert response.status_code == 404


def test_aliased_booking_refuses_without_state_changes(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    _survivor, alias, _decision, _key = _merged_pair(rbac_graph, services)
    client = verified_client(rbac_graph)
    with runtime_role():
        assert (
            client.get(
                reverse("intake:patient-duplicates", args=(rbac_graph.clinic_a,))
            ).status_code
            == 200
        )
    before = _physical_rows()
    session_before = dict(client.session)
    url = reverse("scheduling:appointment-create", args=(rbac_graph.clinic_a,))
    data = create_payload(
        alias.enrollment.pk,
        setup.practitioner_id,
        "2035-06-02T09:00",
        "2035-06-02T10:00",
    )

    with runtime_role():
        response = client.post(url, data)

    assert response.status_code == 409
    assert _physical_rows() == before
    assert dict(client.session) == session_before
    assert alias.patient.full_name.encode() not in response.content
