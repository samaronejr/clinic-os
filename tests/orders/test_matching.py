"""Wrong-patient results never enter chart projections."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.intake.models import Patient
from apps.orders.models import ResultAcknowledgment, ResultMatch, ResultQuarantine
from apps.orders.services import (
    IdentityEvidence,
    ResultInput,
    ingest_result,
    place_order,
)

from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_wrong_patient_upload_is_quarantined_without_chart_change(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a real registered clinician and an order for the enrolled patient.
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        order = place_order(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
        # When: the uploaded report explicitly names somebody else.
        result = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=ResultInput(
                order_id=order.pk,
                identity=IdentityEvidence(
                    birth_date=date(1980, 1, 1), name="Sintetico Outra Pessoa"
                ),
                content_type="application/pdf",
                data=b"%PDF-1.4\nsynthetic result\n%%EOF",
                event_id=uuid4(),
            ),
        )
        # Then: only a quarantine receipt and accountable operator task exist.
        assert result.state == "quarantined"
        assert not ResultMatch.objects.filter(version_id=result.version_id).exists()
        quarantine = ResultQuarantine.objects.get(version_id=result.version_id)
        assert quarantine.reason == "identity_mismatch"
    with owner_context(rbac_graph.organization_a):
        quarantine = ResultQuarantine.objects.get(version_id=result.version_id)
        assert quarantine.work_item.task.owner_role == "receptionist"


def test_two_exact_factors_file_without_implicit_review(
    rbac_graph: RbacGraph,
) -> None:
    # Given: independently loaded identity and one placed order.
    world = chart_world(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        patient = Patient.objects.get(pk=world.patient)
    with as_actor(rbac_graph, rbac_graph.physician):
        order = place_order(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
        # When: order id, date of birth and exact normalized name all match.
        result = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=ResultInput(
                order_id=order.pk,
                identity=IdentityEvidence(
                    birth_date=patient.birth_date, name=patient.full_name
                ),
                content_type="application/pdf",
                data=b"%PDF-1.4\nsynthetic result\n%%EOF",
                event_id=uuid4(),
            ),
        )
        # Then: it is filed but no clinical review has been manufactured.
        assert result.state == "matched"
        assert ResultMatch.objects.filter(version_id=result.version_id).exists()
        assert not ResultAcknowledgment.objects.filter(
            version_id=result.version_id
        ).exists()
