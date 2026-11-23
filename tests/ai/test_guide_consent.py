"""Live Finance decisions disclose a single bit, never the receipt or a chart."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.artifacts import read_artifact
from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.models import AIInvocation
from apps.consent.models import ConsentAcceptance
from apps.core.secrets import secret_store
from apps.insurance.guides import resubmit_guide
from apps.tenancy.envelope import KEK_SECRET_NAME
from django.db import connection

from ai.chart_consent_support import accept_ai
from ai.pending_display_support import test_finance_queue, withdraw
from ai.revenue_support import request_and_collect, revenue_scope
from ai.test_revenue_cycle import finance_setup, world
from billing.journal_support import finance_scope
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "test_finance_queue", "world")
pytestmark = pytest.mark.django_db(transaction=True)


def _binding(clinic: UUID, guide: UUID, encounter: UUID, source: UUID | None) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_guide_consent(%s,%s,%s,%s,%s)",
            [
                clinic,
                guide,
                encounter,
                source,
                secret_store().get_secret(KEK_SECRET_NAME),
            ],
        )
        result = cursor.fetchall()
    assert len(result) == 1
    assert len(result[0]) == 1
    value = result[0][0]
    assert type(value) is bool
    return value


def test_finance_guide_binding_is_one_bit_and_never_rebinds(
    world: RevenueWorld,
) -> None:
    setup = world.setup
    g, guide = setup.graph, world.cycle.guide
    patient, receipt = accept_ai(g, setup.appointment.enrollment_id)
    review = request_and_collect(world)
    assert review.state == "ready"
    assert review.output_id is not None
    assert review.invocation_id is not None
    with finance_scope(setup):
        # The exact acceptance exists but the reviewing Finance role cannot read it.
        assert not ConsentAcceptance.objects.filter(pk=receipt).exists()
        source = AIInvocation.objects.get(pk=review.invocation_id).inputs_ref_id
        assert _binding(g.clinic_a, guide.pk, guide.encounter_id, source)
        assert read_artifact(clinic_id=g.clinic_a, artifact_id=review.output_id)
        for clinic, selected, visit in (
            (g.clinic_b, guide.pk, guide.encounter_id),
            (g.clinic_c, guide.pk, guide.encounter_id),
            (g.clinic_a, uuid4(), guide.encounter_id),
            (g.clinic_a, guide.pk, uuid4()),
        ):
            assert not _binding(clinic, selected, visit, None)
            assert not _binding(clinic, selected, visit, source)
    with revenue_scope(world, world.administrator):
        # Another authorized Finance actor can check the guide, not our input.
        assert _binding(g.clinic_a, guide.pk, guide.encounter_id, None)
        assert not _binding(g.clinic_a, guide.pk, guide.encounter_id, source)
    with revenue_scope(world, g.physician):
        assert not _binding(g.clinic_a, guide.pk, guide.encounter_id, None)
    with runtime_role():
        assert not _binding(g.clinic_a, guide.pk, guide.encounter_id, source)
    withdraw(patient, receipt)
    with finance_scope(setup):
        assert not _binding(g.clinic_a, guide.pk, guide.encounter_id, None)
        assert not _binding(g.clinic_a, guide.pk, guide.encounter_id, source)
    accept_ai(g, setup.appointment.enrollment_id)
    with finance_scope(setup):
        assert _binding(g.clinic_a, guide.pk, guide.encounter_id, None)
        assert not _binding(g.clinic_a, guide.pk, guide.encounter_id, source)
        with pytest.raises(GatewayAccessDeniedError):
            read_artifact(clinic_id=g.clinic_a, artifact_id=review.output_id)
    # New work can be admitted on the replacement receipt. A real successor
    # guide on the very same invoice/visit still cannot borrow that input.
    fresh = request_and_collect(world)
    assert fresh.state == "ready"
    assert fresh.invocation_id is not None
    with finance_scope(setup):
        new_source = AIInvocation.objects.get(pk=fresh.invocation_id).inputs_ref_id
        assert _binding(g.clinic_a, guide.pk, guide.encounter_id, new_source)
        other = resubmit_guide(
            clinic_id=g.clinic_a, guide_id=guide.pk, command_id=uuid4()
        )
        assert _binding(g.clinic_a, other.pk, other.encounter_id, None)
        assert not _binding(g.clinic_a, other.pk, other.encounter_id, new_source)
