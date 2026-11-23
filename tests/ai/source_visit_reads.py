"""Independent source-visit choice for materialized A1 proposals.

Derive the encounter-backed purposes from the actual policy contract, not a
hand-picked list of clinical purpose names. A new A1 tool must be classified
and a new visit-linked capability must supply its own executable read oracle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.action_contracts import ActionInputError
from apps.ai.agent_policies import A1_TOOLS, CARE_TOOLS, REVENUE_TOOLS, PolicyTerms
from apps.ai.care_coordination import CareAccessDeniedError, care_bundle
from apps.ai.contracts import Purpose
from apps.ai.evaluation.services import PURPOSE_CAPABILITIES
from apps.consent.models import ConsentPurpose
from apps.consent.services import consent_for_future_use, record_ai_disclosure
from apps.ehr.models import Encounter
from apps.workflows.care.models import CareAction
from django.urls import reverse

from ai import care_support
from ai.chart_consent_support import accept_ai
from ai.revenue_support import request_and_collect
from ai.test_care_views import _client
from ai.test_revenue_views import post
from billing.journal_support import finance_scope
from core.test_navigation import _get, _post
from ehr.test_autosave import as_actor
from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from ai.revenue_support import RevenueWorld
    from rbac_fixtures import RbacGraph


def visit_proposal_purposes() -> tuple[Purpose, ...]:
    # The operational review checklist has no patient source. Every other A1
    # tool is bound to a plan or guide with an encounter; additions must not
    # disappear merely because this test doesn't yet know their purpose.
    source_tools = CARE_TOOLS | REVENUE_TOOLS
    assert source_tools | {"workflows.review_task"} == A1_TOOLS
    purposes = set()
    reached = set()
    for purpose in map(Purpose, PURPOSE_CAPABILITIES):
        for tool in source_tools:
            try:
                PolicyTerms(purpose.value, "A1", (tool,), enabled=True)
            except ActionInputError:
                continue
            purposes.add(purpose)
            reached.add(tool)
    assert reached == source_tools
    return tuple(sorted(purposes))


def _three_pending(
    g: RbacGraph, patch: pytest.MonkeyPatch, secret: Path, reader: str
) -> None:
    care = care_support.care_world(g, patch, secret)
    _patient, receipt = accept_ai(g, care.plan.enrollment_id)
    bundle = care_support.request_and_collect(care)
    assert bundle.invocation_id is not None
    assert len(bundle.items) == 3
    assert all(i.state == "pending" for i in bundle.items)
    with owner_context(g.organization_a):
        other = Encounter.objects.create(
            organization_id=g.organization_a,
            clinic_id=g.clinic_a,
            patient_id=care.plan.enrollment.patient_id,
            physician_id=g.physician,
            unscheduled_reason="documentation_only",
        )
        existing_actions = set(
            CareAction.objects.filter(plan=care.plan).values_list("pk", flat=True)
        )
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a, encounter_id=other.pk, informed=True, refused=True
        )
        assert (
            len(
                care_bundle(
                    clinic_id=g.clinic_a, invocation_id=bundle.invocation_id
                ).items
            )
            == 3
        )
    manager = _client(care.manager, "clinic_manager")
    url = reverse("ai:care-coordination", args=(g.clinic_a,))
    assert str(bundle.invocation_id).encode() in _get(manager, url).content
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a,
            encounter_id=care.plan.encounter_id,
            informed=True,
            refused=True,
        )
        active = consent_for_future_use(
            clinic_id=g.clinic_a,
            enrollment_id=care.plan.enrollment_id,
            purpose=ConsentPurpose.AI_ASSISTANCE,
        )
        assert active is not None
        assert active.pk == receipt
    if reader == "queue":
        page = _get(manager, url)
        assert page.status_code == 200
        assert str(bundle.invocation_id).encode() not in page.content
        denied = _post(
            manager,
            url,
            {"action": "preview", "invocation_id": str(bundle.invocation_id)},
        )
        assert denied.status_code == 403
        assert b"data-care-preview-item=" not in denied.content
    else:
        with as_actor(g, g.physician), pytest.raises(CareAccessDeniedError):
            care_bundle(clinic_id=g.clinic_a, invocation_id=bundle.invocation_id)
    with owner_context(g.organization_a):
        assert (
            set(CareAction.objects.filter(plan=care.plan).values_list("pk", flat=True))
            == existing_actions
        )


def _guide_pending(care: RevenueWorld) -> None:
    g, guide = care.setup.graph, care.cycle.guide
    accept_ai(g, care.setup.appointment.enrollment_id)
    review = request_and_collect(care)
    assert review.state == "ready"
    assert review.invocation_id is not None
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a,
            encounter_id=guide.encounter_id,
            informed=True,
            refused=True,
        )
    client = verified_client(g)
    url = reverse("ai:revenue-cycle", args=(g.clinic_a,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
    response = post(
        client, url, {"command": "refresh", "invocation_id": str(review.invocation_id)}
    )
    assert response.status_code == 200
    assert not response.context["review"].items
    with finance_scope(care.setup):
        assert type(guide).objects.filter(pk=guide.pk).exists()


def check_source_visit(
    purpose: Purpose,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    reader: str,
) -> None:
    if purpose is Purpose.CARE_COORDINATION:
        _three_pending(
            rbac_graph,
            monkeypatch,
            request.getfixturevalue("synthetic_secret_backend"),
            reader,
        )
    elif purpose is Purpose.REVENUE_CYCLE:
        _guide_pending(request.getfixturevalue("world"))
    else:
        pytest.fail(f"No source-encounter refusal oracle for {purpose.value}")
