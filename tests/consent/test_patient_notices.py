"""Current information notices through the patient request boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.consent.models import ConsentAcceptance, NoticeTopic, RefusalRecord
from apps.consent.services import publish_notice
from apps.tenancy.db import tenant_context
from django.test import Client

from otp_test_support import runtime_role as http_runtime_role
from patient_service_support import runtime_role
from renewal.test_consent import seed
from renewal.test_encounters import setup_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_patient_reads_current_notice_without_optional_decisions(
    rbac_graph: RbacGraph,
) -> None:
    # Given an informational notice and an authenticated patient session.
    graph = rbac_graph
    _, patient_session_id, _, manager_id = seed(graph)
    with runtime_role(), tenant_context(manager_id, graph.organization_a):
        notice = publish_notice(
            clinic_id=graph.clinic_a,
            topic=NoticeTopic.CARE_PROCESSING,
            text="Aviso informativo sobre o tratamento de dados no atendimento.",
        )
    with setup_context(graph.organization_a):
        before = (
            ConsentAcceptance.objects.count(),
            RefusalRecord.objects.count(),
        )
    client = Client()
    browser_session = client.session
    browser_session["patient_session_id"] = str(patient_session_id)
    browser_session.save()

    # When the patient opens the actual consent page.
    with http_runtime_role():
        response = client.get("/patient/consent/")

    # Then the current notice is readable without recording a decision.
    assert response.status_code == 200
    assert [row.pk for row in response.context["notices"]] == [notice.pk]
    with setup_context(graph.organization_a):
        assert (
            ConsentAcceptance.objects.count(),
            RefusalRecord.objects.count(),
        ) == before
