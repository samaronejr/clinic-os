"""Owner-provisioned synthetic gate fixtures; runtime grants are never changed."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.decision_policy import GATE_CAPABILITIES
from apps.ai.decision_support import display_decision_support, request_decision_support
from apps.ai.models import AIInvocation
from apps.ai.policy import OrganizationPolicy
from apps.ai.tasks import run_operation
from apps.consent.services import record_ai_disclosure
from apps.identity.clinic_configuration import latest_configuration
from apps.providers.lifecycle import (
    ApprovalInput,
    VersionInput,
    approve_version,
    propose_version,
    revoke_version,
)
from django.db import connection, transaction

from ai.brief_support import BINDING, BriefWorld, seeded
from ai.support import model
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    import pytest
    from apps.ai.decision_support import DecisionPanel
    from apps.providers.models import CapabilityVersion

    from rbac_fixtures import RbacGraph


def world_for(graph: RbacGraph, monkeypatch: pytest.MonkeyPatch) -> BriefWorld:
    world = seeded(graph, monkeypatch)
    model(
        organization=graph.organization_a,
        clinic=graph.clinic_a,
        purpose=Purpose.DECISION_SUPPORT,
    )
    return world


def gate(world: BriefWorld, key: str, record: str) -> CapabilityVersion:
    """Only simulated owner evidence is installed in the disposable test database."""
    clinic = world.graph.clinic_a
    propose_version(
        key,
        clinic_id=clinic,
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
            record_ref=record,
        ),
    )
    version = approve_version(
        key,
        clinic_id=clinic,
        decision=ApprovalInput(
            "Synthetic gate fixture",
            "Synthetic owner; not an external approval",
            f"https://synthetic.invalid/{record}",
        ),
    )
    with transaction.atomic():
        for state in ("sandbox", "production_authorized"):
            version.state = state
            version.save(update_fields=("state", "updated_at"))
    return version


def revoke_gate(world: BriefWorld) -> None:
    revoke_version(
        GATE_CAPABILITIES[0][0],
        clinic_id=world.graph.clinic_a,
        decision=ApprovalInput(
            "Synthetic owner", "Synthetic fixture", "https://synthetic.invalid/revoked"
        ),
        reason="Synthetic clinical safety approval revoked",
    )


def enable(world: BriefWorld, *, cap: int = 10_000_000) -> None:
    g = world.graph
    with as_actor(g, world.gateway.administrator):
        current = latest_configuration(g.clinic_a)
        assert current is not None
        set_organization_policy(
            clinic_id=g.clinic_a,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=("brief", "decision_support"),
                monthly_budget_micros=cap,
                purpose_budgets=(("brief", cap), ("decision_support", cap)),
            ),
        )
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a,
            encounter_id=world.chart.version.document.encounter_id,
            informed=True,
            refused=False,
        )
    for key, record in GATE_CAPABILITIES:
        gate(world, key, record)


def generate(world: BriefWorld, *, opt_in: bool = True) -> DecisionPanel:
    g = world.graph
    with as_actor(g, g.physician):
        return request_decision_support(
            clinic_id=g.clinic_a,
            encounter_id=world.chart.version.document.encounter_id,
            session_binding=BINDING,
            opt_in=opt_in,
        )


def finish(world: BriefWorld, queued: DecisionPanel) -> str:
    assert queued.invocation_id is not None
    with owner_context(world.graph.organization_a):
        operation = AIInvocation.objects.get(pk=queued.invocation_id).operation_id
    assert operation is not None
    with runtime_role():
        return run_operation(operation_id=str(operation))


def display(world: BriefWorld, queued: DecisionPanel) -> DecisionPanel:
    assert queued.invocation_id is not None
    g = world.graph
    with as_actor(g, g.physician):
        return display_decision_support(
            clinic_id=g.clinic_a,
            encounter_id=world.chart.version.document.encounter_id,
            session_binding=BINDING,
            invocation_id=queued.invocation_id,
        )


def counts(world: BriefWorld, *, database_url: str | None = None) -> tuple[int, ...]:
    query = (
        "SELECT (SELECT count(*) FROM clinic_app.ai_aiartifact), "
        "(SELECT count(*) FROM clinic_app.ai_aiinvocation), "
        "(SELECT count(*) FROM clinic_app.comms_integrationoperation), "
        "(SELECT count(*) FROM clinic_app.audit_event)"
    )
    row: tuple[int, ...] | None
    if database_url is not None:
        with psycopg.connect(database_url) as inspection:
            row = inspection.execute(query).fetchone()
    else:
        with owner_context(world.graph.organization_a), connection.cursor() as cursor:
            cursor.execute(query)
            row = cursor.fetchone()
    assert row is not None
    return row
