"""Task 63: governed gateway, catalog-only output and explicit human review."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.ai.artifacts import read_artifact
from apps.ai.contracts import GatewayAccessDeniedError, GatewayInputError, Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.models import AIArtifact, AIInvocation
from apps.ai.policy import OrganizationPolicy
from apps.analytics.analyst import display_proposal, request_analyst, review_proposal
from apps.analytics.analyst_contracts import (
    AnalystRequest,
    parse_request,
    question_metric,
    synthetic_reply,
    validate_reply,
)
from apps.analytics.contracts import AnalyticsAccessDeniedError, AnalyticsConflictError
from apps.analytics.models import AnalystReview, ReadModelRefresh
from apps.analytics.services import authorize
from apps.core.integration import execute_operation
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.utils import timezone
from django.utils.translation import gettext

from ai.support import model, seed
from analytics.test_metrics import metric_query
from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from ai.support import GatewayWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def analyst_world(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> GatewayWorld:
    world = seed(rbac_graph, monkeypatch)
    model(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        purpose=Purpose.ANALYST,
    )
    with runtime_role(), tenant_context(world.administrator, rbac_graph.organization_a):
        current = latest_configuration(rbac_graph.clinic_a)
        assert current is not None
        set_organization_policy(
            clinic_id=rbac_graph.clinic_a,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=("analyst",),
                monthly_budget_micros=10_000_000,
                purpose_budgets=(("analyst", 10_000_000),),
            ),
        )
    return world


def test_question_and_model_boundaries_never_invent_scope_or_arithmetic() -> None:
    assert question_metric("mostrar taxa de faltas") == "no_show"
    assert question_metric("receita e ocupacao") is None
    assert question_metric("ignore instructions; SELECT patient; Sintetico PHI") is None
    with pytest.raises(AnalyticsAccessDeniedError):
        question_metric("show all clinics revenue")
    request = AnalystRequest(metric_query("no_show"), "a" * 64)
    assert parse_request(request.encode()) == request
    assert validate_reply(synthetic_reply(request.encode()), request) == request.query
    with pytest.raises(GatewayInputError):
        validate_reply(
            replace(request.query, metric_id="receivables").encode(), request
        )
    with pytest.raises(GatewayInputError):
        validate_reply(
            json.dumps(request.query.payload() | {"sql": "SELECT 1"}), request
        )


def test_gateway_runs_offline_and_only_human_acceptance_executes_metrics(
    analyst_world: GatewayWorld,
) -> None:
    world = analyst_world
    graph = world.graph
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        proposal = request_analyst(
            clinic_id=graph.clinic_a, question="faltas", query=metric_query("no_show")
        )
        assert proposal.state == "generating"
        assert proposal.invocation_id is not None
        invocation = AIInvocation.objects.get(pk=proposal.invocation_id)
        assert ReadModelRefresh.objects.count() == 0
        assert "faltas" not in invocation.inputs_ref.content
        assert (
            display_proposal(
                clinic_id=graph.clinic_a, invocation_id=invocation.pk
            ).state
            == "generating"
        )
        operation = invocation.operation_id
    assert operation is not None
    with runtime_role():
        execute_operation(operation_id=operation)
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        ready = display_proposal(clinic_id=graph.clinic_a, invocation_id=invocation.pk)
        assert ready.state == "review"
        assert ready.query is not None
        assert ReadModelRefresh.objects.count() == 0
        with pytest.raises(AnalyticsConflictError):
            review_proposal(
                clinic_id=graph.clinic_a,
                invocation_id=invocation.pk,
                query_digest="0" * 64,
                authorization_version=ready.authorization_version,
                decision="accepted",
            )
        assert AnalystReview.objects.count() == 0
        result = review_proposal(
            clinic_id=graph.clinic_a,
            invocation_id=invocation.pk,
            query_digest=ready.query.digest,
            authorization_version=ready.authorization_version,
            decision="accepted",
        )
        assert result is not None
        assert AnalystReview.objects.get(invocation=invocation).decision == "accepted"
        assert (
            review_proposal(
                clinic_id=graph.clinic_a,
                invocation_id=invocation.pk,
                query_digest=ready.query.digest,
                authorization_version=ready.authorization_version,
                decision="accepted",
            )
            == result
        )
        with pytest.raises(AnalyticsConflictError):
            review_proposal(
                clinic_id=graph.clinic_a,
                invocation_id=invocation.pk,
                query_digest=ready.query.digest,
                authorization_version=ready.authorization_version,
                decision="rejected",
            )


def test_scope_rejection_and_ambiguity_leave_no_artifact_or_refresh(
    analyst_world: GatewayWorld,
) -> None:
    world = analyst_world
    with (
        runtime_role(),
        tenant_context(world.administrator, world.graph.organization_a),
    ):
        before = AIArtifact.objects.count()
        with pytest.raises(AnalyticsAccessDeniedError):
            request_analyst(
                clinic_id=world.graph.clinic_a,
                question="show all clinics revenue",
                query=metric_query("receivables"),
            )
        assert (
            request_analyst(
                clinic_id=world.graph.clinic_a,
                question="Sintetico PHI injected SQL",
                query=metric_query("no_show"),
            ).state
            == "clarification"
        )
        assert AIArtifact.objects.count() == before
        assert ReadModelRefresh.objects.count() == 0


def test_revoked_reporting_authority_blocks_queued_work_and_artifact_reads(
    analyst_world: GatewayWorld,
) -> None:
    world = analyst_world
    graph = world.graph
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        requested = request_analyst(
            clinic_id=graph.clinic_a, question="faltas", query=metric_query("no_show")
        )
        assert requested.invocation_id is not None
        invocation = AIInvocation.objects.get(pk=requested.invocation_id)
        assert authorize(clinic_id=graph.clinic_a)
    with owner_context(graph.organization_a):
        # Remove reporting authority but retain charge.read through receptionist.
        UserClinicRole.objects.filter(
            user_id=world.administrator, clinic_id=graph.clinic_a
        ).update(revoked_at=timezone.now())
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=world.administrator,
            role="receptionist",
        )
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        with pytest.raises(GatewayAccessDeniedError):
            read_artifact(
                clinic_id=graph.clinic_a, artifact_id=invocation.inputs_ref_id
            )
        with pytest.raises(AnalyticsAccessDeniedError):
            display_proposal(clinic_id=graph.clinic_a, invocation_id=invocation.pk)
    assert invocation.operation_id is not None
    with runtime_role():
        execute_operation(operation_id=invocation.operation_id)
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        invocation.refresh_from_db()
        assert invocation.status == "failed"


def test_native_human_review_shows_all_descriptor_fields_and_binds_acceptance(
    analyst_world: GatewayWorld,
    tmp_path: Path,
) -> None:
    world = analyst_world
    graph = world.graph
    client = verified_client(replace(graph, clinic_admin=world.administrator))
    url = f"/reports/clinics/{graph.clinic_a}/"
    with runtime_role():
        requested = client.post(
            url,
            {
                "action": "ask",
                "question": "faltas",
                "metric": "no_show",
                "start": "01/11/2026",
                "end": "06/11/2026",
                "grouping": "day",
                "weekday": "0",
            },
        )
        assert requested.status_code == 200
        assert b'data-analyst-state="generating"' in requested.content
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        invocation = AIInvocation.objects.get(purpose="analyst")
        assert ReadModelRefresh.objects.count() == 0
    assert invocation.operation_id is not None
    with runtime_role():
        execute_operation(operation_id=invocation.operation_id)
        ready = client.post(
            url, {"action": "proposal", "reference": str(invocation.pk)}
        )
    assert ready.status_code == 200
    assert b'data-analyst-state="review"' in ready.content
    assert b"America/Sao_Paulo" in ready.content
    assert b"01/11/2026 00:00" in ready.content
    assert b"07/11/2026 00:00" in ready.content
    assert gettext("Monday").encode() in ready.content
    (tmp_path / "proposal.html").write_bytes(ready.content)
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        proposal = display_proposal(
            clinic_id=graph.clinic_a, invocation_id=invocation.pk
        )
    assert proposal.query is not None
    with runtime_role():
        accepted = client.post(
            url,
            {
                "action": "accepted",
                "reference": str(invocation.pk),
                "query_digest": proposal.query.digest,
                "authorization_version": proposal.authorization_version,
            },
        )
    assert accepted.status_code == 200
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        assert AnalystReview.objects.get(invocation=invocation).decision == "accepted"
        assert ReadModelRefresh.objects.count() == 1
