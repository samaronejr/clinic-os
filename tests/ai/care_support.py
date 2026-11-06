"""Real-role care-coordination world: finalized plan text, gateway and agent policy."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.ai.agent_policies import PolicyTerms, publish_agent_policy
from apps.ai.attestations import step_up_evidence
from apps.ai.care_coordination import (
    CareBundle,
    approve_care_bundle,
    bundle_digest,
    collect_care_coordination,
    pending_bundle,
    request_care_coordination,
)
from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.models import AIInvocation
from apps.ai.policy import OrganizationPolicy
from apps.core.integration import execute_operation
from apps.ehr.finalization import finalize_version
from apps.ehr.services import record_clinical_note
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import UserClinicRole
from apps.providers.lifecycle import VersionInput, propose_version
from apps.tenancy.db import tenant_context
from apps.workflows.care import services as care
from apps.workflows.care.validation import CareActionSpec, return_window
from django.utils import timezone

from ai.support import GatewayWorld, model, seed
from auth.stepup_test_support import verified_request
from ehr.longitudinal_support import chart_world
from ehr.test_autosave import SECTIONS, as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from workflows.permission_support import permission_actor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import pytest
    from apps.ai.care_coordination import ItemOutcome
    from apps.ai.models import AgentPolicyVersion
    from apps.ehr.models import ClinicalDocumentVersion
    from apps.workflows.care.models import CarePlan

    from rbac_fixtures import RbacGraph

# The reviewed synthetic plan sections replayed by care-coordination-v1.json.
BUNDLE_PLAN = (
    "Retorno em 6 semanas. Recepção confirma o agendamento do retorno. "
    "Reaplicar o PHQ-9 em 2 semanas."
)
INVALID_PLAN = (
    "Retorno em 6 semanas. Recepção confirma o agendamento do retorno. "
    "Revisão anual em 3 anos."
)
MEDICATION_PLAN = "Retorno em 6 semanas. Manter o acompanhamento combinado na consulta."
MEDICATION_SENTINEL = "SINTETICO-SENTINELA-MEDICACAO"
BUDGET_MICROS = 10_000_000


@dataclass(frozen=True, slots=True)
class CareWorld:
    graph: RbacGraph
    gateway: GatewayWorld
    physician: UUID
    manager: UUID
    version: ClinicalDocumentVersion
    plan: CarePlan
    policy: AgentPolicyVersion


@contextmanager
def acting(graph: RbacGraph, user: UUID) -> Iterator[None]:
    with runtime_role(), tenant_context(user, graph.organization_a):
        yield


def finalized_plan(
    graph: RbacGraph, version: ClinicalDocumentVersion, text: str
) -> ClinicalDocumentVersion:
    request = verified_request(version.author_id)
    with as_actor(graph, version.author_id):
        saved = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=version.revision,
            content={**SECTIONS, "plan": text},
        )
        return finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=saved.revision,
            request=request,
        )


def care_world(
    graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    secret_root: Path,
    *,
    text: str = BUNDLE_PLAN,
) -> CareWorld:
    secret = secret_root / "agent-attestation-v1.secret"
    secret.write_text(secrets.token_hex(32))
    secret.chmod(0o600)
    gateway = seed(graph, monkeypatch)
    model(
        organization=graph.organization_a,
        clinic=graph.clinic_a,
        purpose=Purpose.CARE_COORDINATION,
    )
    with acting(graph, gateway.administrator):
        current = latest_configuration(graph.clinic_a)
        assert current is not None
        set_organization_policy(
            clinic_id=graph.clinic_a,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=(Purpose.FRONT_DESK.value, Purpose.CARE_COORDINATION.value),
                monthly_budget_micros=BUDGET_MICROS,
                purpose_budgets=(
                    (Purpose.FRONT_DESK.value, BUDGET_MICROS),
                    (Purpose.CARE_COORDINATION.value, BUDGET_MICROS),
                ),
            ),
        )
    propose_version(
        "email",
        version_input=VersionInput(
            provider="synthetic",
            account=f"synthetic-{uuid4().hex}",
            environment="synthetic",
            api_version="v1",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    manager, _enrollment = permission_actor(graph, "clinic_manager")
    with acting(graph, manager):
        policy = publish_agent_policy(
            clinic_id=graph.clinic_a,
            expected_version=0,
            terms=PolicyTerms(
                "care_coordination",
                "A1",
                ("care_action.create",),
                enabled=True,
            ),
        )
    chart = chart_world(graph)
    version = finalized_plan(graph, chart.version, text)
    # The care services need the workflow candidate bundle (tasks.*), as in
    # the workflows test population.
    for organization in (graph.organization_a, graph.organization_b):
        with owner_context(organization):
            UserClinicRole.objects.filter(organization_id=organization).update(
                workflow_bundle_version=2
            )
    start, end = return_window(now=timezone.now(), weeks=2)
    with acting(graph, version.author_id):
        plan = care.create_care_plan_from_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            actions=(
                CareActionSpec(
                    kind="return_window", window_start=start, window_end=end
                ),
            ),
            idempotency_key=uuid4(),
        )
    return CareWorld(graph, gateway, version.author_id, manager, version, plan, policy)


def run_model(world: CareWorld, invocation_id: UUID) -> str:
    """Execute the queued synthetic invocation as the worker does."""
    with owner_context(world.graph.organization_a):
        operation = AIInvocation.objects.get(pk=invocation_id).operation_id
    assert operation is not None
    with runtime_role():
        return execute_operation(operation_id=operation)


def request_and_collect(world: CareWorld) -> CareBundle:
    graph = world.graph
    with acting(graph, world.physician):
        requested = request_care_coordination(
            clinic_id=graph.clinic_a, plan_id=world.plan.pk
        )
    assert requested.state == "generating"
    assert requested.invocation_id is not None
    assert run_model(world, requested.invocation_id) == "succeeded"
    with acting(graph, world.physician):
        return collect_care_coordination(
            clinic_id=graph.clinic_a, invocation_id=requested.invocation_id
        )


def approve_bundle(world: CareWorld, invocation_id: UUID) -> tuple[ItemOutcome, ...]:
    graph = world.graph
    request = verified_request(world.manager)
    with acting(graph, world.manager):
        items = pending_bundle(clinic_id=graph.clinic_a, invocation_id=invocation_id)
        digest = bundle_digest(
            clinic_id=graph.clinic_a, invocation_id=invocation_id, items=items
        )
        return approve_care_bundle(
            clinic_id=graph.clinic_a,
            invocation_id=invocation_id,
            digest=digest,
            step_up=lambda value: step_up_evidence(
                request, clinic_id=graph.clinic_a, digest=value
            ),
        )
