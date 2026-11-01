"""Native and enhanced extraction refusals after real job authorization."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.extraction import collect_extraction, start_extraction
from apps.core.integration import execute_operation
from django.conf import settings
from django.urls import reverse

from ai.refusal_support import refusal_state
from ai.support import DEFAULT_BUDGET
from ai.test_extraction_runtime import extracted_job
from ehr.test_addenda import signed_in
from ehr.test_autosave import as_actor
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "htmx"])
def test_extraction_candidate_must_belong_to_the_authorized_job(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
    enhanced: bool,
) -> None:
    graph = rbac_graph
    job = extracted_job(graph, monkeypatch)
    with as_actor(graph, graph.physician):
        sibling = start_extraction(
            clinic_id=job.clinic_id,
            result_version_id=job.result_version_id,
            budget=DEFAULT_BUDGET,
        )
        operation = sibling.invocation.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation_id=operation) == "succeeded"
    with as_actor(graph, graph.physician):
        collect_extraction(clinic_id=job.clinic_id, job_id=sibling.pk)
        candidate = sibling.candidates.order_by("position").first()
        assert candidate is not None
        assert not job.candidates.filter(pk=candidate.pk).exists()
    with signed_in(graph.physician) as client:
        session = dict(client.session)
        before = refusal_state(superuser_database_url)
        bodies = []
        for action in ("confirmed", "rejected"):
            for identifier in (candidate.pk, uuid4()):
                response = client.post(
                    reverse("ai:extraction", args=(job.clinic_id,)),
                    {
                        "action": action,
                        "job_id": str(job.pk),
                        "candidate_id": str(identifier),
                    },
                    HTTP_HX_REQUEST="true" if enhanced else "false",
                )
                assert response.status_code == 403
                assert response.wsgi_request.session.modified is False
                assert dict(client.session) == session
                assert settings.SESSION_COOKIE_NAME not in response.cookies
                assert str(identifier).encode() not in response.content
                assert str(job.pk).encode() not in response.content
                assert refusal_state(superuser_database_url) == before
                bodies.append(response.content)
        assert all(body == bodies[0] for body in bodies)


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "htmx"])
def test_extraction_invalid_action_refuses_after_job_authorization(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
    enhanced: bool,
) -> None:
    graph = rbac_graph
    job = extracted_job(graph, monkeypatch)
    with signed_in(graph.physician) as client:
        session = dict(client.session)
        before = refusal_state(superuser_database_url)
        bodies = []
        for action in ("unknown", "open"):
            response = client.post(
                reverse("ai:extraction", args=(job.clinic_id,)),
                {"action": action, "job_id": str(job.pk)},
                HTTP_HX_REQUEST="true" if enhanced else "false",
            )
            assert response.status_code == 400
            assert response.wsgi_request.session.modified is False
            assert dict(client.session) == session
            assert settings.SESSION_COOKIE_NAME not in response.cookies
            assert str(job.pk).encode() not in response.content
            assert refusal_state(superuser_database_url) == before
            bodies.append(response.content)
        assert bodies[0] == bodies[1]
