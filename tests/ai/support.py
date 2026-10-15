"""Real-role gateway setup; only the broker publication seam is recorded."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.ai import tasks
from apps.ai.artifacts import create_input
from apps.ai.contracts import Budget, Price, Purpose, Usage
from apps.ai.controls import set_organization_policy
from apps.ai.models import ModelConfiguration
from apps.ai.owner import ModelTerms, propose_model, set_global_switch, transition_model
from apps.ai.policy import OrganizationPolicy
from apps.ai.services import invoke
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    publish_configuration,
)
from apps.identity.models import User, UserClinicRole
from apps.providers.lifecycle import VersionInput, propose_version
from apps.tenancy.db import tenant_context
from django.apps import apps

from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    import pytest
    from apps.ai.contracts import InvocationResult

    from rbac_fixtures import RbacGraph

SENTINEL = "SINTETICO-SENTINELA-AI-CONTENT"
DEFAULT_BUDGET = Budget(
    Usage(tokens_in=100, tokens_out=100, audio_seconds=30), maximum_micros=10_000
)


@dataclass(frozen=True, slots=True)
class GatewayWorld:
    graph: RbacGraph
    administrator: UUID
    configuration: ModelConfiguration
    publications: list[tuple[dict[str, str], str, int]]


def model(
    *,
    purpose: Purpose = Purpose.FRONT_DESK,
    fallback: bool = False,
    region: str = "br-synthetic",
) -> ModelConfiguration:
    if not fallback:
        for previous in ModelConfiguration.objects.filter(
            purpose=purpose.value, state="active", route="primary"
        ):
            transition_model(configuration_id=previous.pk, state="retired")
    terms = ModelTerms(
        purpose=purpose,
        provider="recorded_fixture" if fallback else "synthetic",
        model_id="fixture-v1" if fallback else "synthetic-v1",
        region=region,
        retention_terms="synthetic-no-retention",
        training_terms="synthetic-no-training",
        price=Price(3, 5, 1, 7),
        route="fallback" if fallback else "primary",
    )
    configuration = propose_model(terms=terms)
    transition_model(
        configuration_id=configuration.pk,
        state="approved",
        approval_ref="synthetic-model-review-v1",
    )
    return transition_model(configuration_id=configuration.pk, state="active")


def policy(*, cap: int = 10_000_000, enabled: bool = True) -> OrganizationPolicy:
    return OrganizationPolicy(
        enabled=enabled,
        purposes=(Purpose.FRONT_DESK.value,),
        monthly_budget_micros=cap,
        purpose_budgets=((Purpose.FRONT_DESK.value, cap),),
    )


def seed(graph: RbacGraph, monkeypatch: pytest.MonkeyPatch) -> GatewayWorld:
    apps.get_app_config("ai").ready()
    publications: list[tuple[dict[str, str], str, int]] = []

    def publish(*, kwargs: dict[str, str], queue: str, countdown: int = 0) -> None:
        publications.append((kwargs, queue, countdown))

    monkeypatch.setattr(tasks.execute, "apply_async", publish)
    administrator = User.objects.create(username=f"synthetic-ai-owner-{uuid4().hex}")
    with owner_context(graph.organization_a):
        for clinic in (graph.clinic_a, graph.clinic_b):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=clinic,
                user=administrator,
                role="owner",
            )
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    configuration = model()
    with runtime_role(), tenant_context(administrator.pk, graph.organization_a):
        first = publish_configuration(
            clinic_id=graph.clinic_a,
            expected_version=0,
            content=ConfigurationContent("Synthetic AI clinic"),
        )
        set_organization_policy(
            clinic_id=graph.clinic_a, expected_version=first.version, policy=policy()
        )
    return GatewayWorld(graph, administrator.pk, configuration, publications)


def set_policy(world: GatewayWorld, *, cap: int, enabled: bool = True) -> None:
    from apps.identity.clinic_configuration import (  # noqa: PLC0415
        latest_configuration,
    )

    with (
        runtime_role(),
        tenant_context(world.administrator, world.graph.organization_a),
    ):
        current = latest_configuration(world.graph.clinic_a)
        assert current is not None
        set_organization_policy(
            clinic_id=world.graph.clinic_a,
            expected_version=current.version,
            policy=replace(policy(cap=cap), enabled=enabled),
        )


def enqueue(world: GatewayWorld) -> InvocationResult:
    g = world.graph
    with runtime_role(), tenant_context(g.shared_user, g.organization_a):
        source = create_input(
            clinic_id=g.clinic_a, purpose=Purpose.FRONT_DESK, content=SENTINEL
        )
        return invoke(
            clinic_id=g.clinic_a,
            purpose=Purpose.FRONT_DESK,
            principal=g.shared_user,
            inputs_ref=source.pk,
            budget=DEFAULT_BUDGET,
        )
