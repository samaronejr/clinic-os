"""Synthetic chart/gateway fixtures with unchanged runtime grants and real RLS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.ai.brief import Brief, display_brief, request_brief
from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.models import AIInvocation
from apps.ai.policy import OrganizationPolicy
from apps.ai.tasks import run_operation
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import UserClinicRole

from ai.support import GatewayWorld, model, seed
from ehr.longitudinal_support import ChartWorld, chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    import pytest

    from rbac_fixtures import RbacGraph

BINDING = "1" * 64


@dataclass(frozen=True, slots=True)
class BriefWorld:
    graph: RbacGraph
    gateway: GatewayWorld
    chart: ChartWorld


def seeded(graph: RbacGraph, monkeypatch: pytest.MonkeyPatch) -> BriefWorld:
    chart = chart_world(graph)
    gateway = seed(graph, monkeypatch)
    model(purpose=Purpose.BRIEF)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(
            clinic_id=graph.clinic_a, user_id=graph.physician
        ).update(workflow_bundle_version=2)
    with as_actor(graph, gateway.administrator):
        configuration = latest_configuration(graph.clinic_a)
        assert configuration is not None
        set_organization_policy(
            clinic_id=graph.clinic_a,
            expected_version=configuration.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=("brief",),
                monthly_budget_micros=10_000_000,
                purpose_budgets=(("brief", 10_000_000),),
            ),
        )
    monkeypatch.setattr("apps.ai.tasks.fair_acquire", lambda **_kwargs: True)
    return BriefWorld(graph, gateway, chart)


def generate(
    world: BriefWorld, *, user: UUID | None = None, question: str = ""
) -> Brief:
    g = world.graph
    with as_actor(g, user or g.physician):
        return request_brief(
            clinic_id=g.clinic_a,
            enrollment_id=world.chart.enrollment,
            session_binding=BINDING,
            question=question,
        )


def finish(world: BriefWorld, queued: Brief) -> None:
    assert queued.state == "generating"
    assert queued.invocation_id is not None
    with owner_context(world.graph.organization_a):
        invocation = AIInvocation.objects.get(pk=queued.invocation_id)
        operation_id = invocation.operation_id
    assert operation_id is not None
    with runtime_role():
        run_operation(operation_id=str(operation_id))


def display(world: BriefWorld, queued: Brief, *, user: UUID | None = None) -> Brief:
    assert queued.invocation_id is not None
    with as_actor(world.graph, user or world.graph.physician):
        return display_brief(
            clinic_id=world.graph.clinic_a,
            enrollment_id=world.chart.enrollment,
            session_binding=BINDING,
            invocation_id=queued.invocation_id,
        )
