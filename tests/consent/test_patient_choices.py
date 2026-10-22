"""Explicit patient decisions through the real consent request boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.consent.models import (
    ConsentAcceptance,
    ConsentPurpose,
    RefusalRecord,
)
from apps.consent.services import publish_text
from apps.intake.models import PatientClinicEnrollment
from apps.tenancy.db import tenant_context
from django.test import Client

from otp_test_support import runtime_role as http_runtime_role
from patient_service_support import runtime_role
from renewal.test_consent import seed
from renewal.test_encounters import setup_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "purpose",
    [
        ConsentPurpose.CONSULTATION_RECORDING,
        ConsentPurpose.AI_ASSISTANCE,
        ConsentPurpose.TRANSACTIONAL_MESSAGING,
        ConsentPurpose.MARKETING,
        ConsentPurpose.RESEARCH_MODEL_IMPROVEMENT,
    ],
)
def test_patient_refusal_is_explicit_and_bound_to_the_offered_purpose(
    rbac_graph: RbacGraph, purpose: str
) -> None:
    graph = rbac_graph
    _, patient_session_id, enrollment_id, manager_id = seed(graph)
    with setup_context(graph.organization_a):
        patient_id = PatientClinicEnrollment.objects.get(pk=enrollment_id).patient_id
    with runtime_role(), tenant_context(manager_id, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose=purpose,
            text="Decisão independente para este propósito.",
            language="pt-BR",
        )
    client = Client()
    browser_session = client.session
    browser_session["patient_session_id"] = str(patient_session_id)
    browser_session.save()
    with http_runtime_role():
        offered = client.post(
            "/patient/consent/", {"action": "read", "text_id": str(text.pk)}
        )
    assert offered.status_code == 200
    offer = offered.context["form"].initial["offer"]
    with http_runtime_role():
        rejected = client.post(
            "/patient/consent/",
            {"action": "refuse", "offer": offer, "purpose": purpose, "accepted": "on"},
        )
    assert rejected.status_code == 400
    with setup_context(graph.organization_a):
        assert not RefusalRecord.objects.filter(enrollment_id=enrollment_id).exists()
        assert not ConsentAcceptance.objects.filter(
            enrollment_id=enrollment_id
        ).exists()
    with http_runtime_role():
        refused = client.post(
            "/patient/consent/",
            {"action": "refuse", "offer": offer, "purpose": purpose},
        )
    assert refused.status_code == 200
    with setup_context(graph.organization_a):
        row = RefusalRecord.objects.get(enrollment_id=enrollment_id)
        assert row.text.purpose == purpose
        assert row.text_id == text.pk
        assert row.patient_session_id == patient_session_id
        assert row.patient_id == patient_id
        assert not ConsentAcceptance.objects.filter(
            enrollment_id=enrollment_id
        ).exists()
    with http_runtime_role():
        offered_again = client.post(
            "/patient/consent/", {"action": "read", "text_id": str(text.pk)}
        )
        accepted = client.post(
            "/patient/consent/",
            {
                "action": "accept",
                "offer": offered_again.context["form"].initial["offer"],
                "purpose": purpose,
                "accepted": "on",
            },
        )
    assert accepted.status_code == 200
    with setup_context(graph.organization_a):
        receipt = ConsentAcceptance.objects.get(enrollment_id=enrollment_id)
        assert receipt.text_id == text.pk
        assert receipt.text.purpose == purpose
        assert RefusalRecord.objects.filter(pk=row.pk).exists()
