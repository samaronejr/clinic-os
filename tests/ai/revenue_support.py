"""Synthetic insurance/journal facts, real finance authority and governed inference."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4
from xml.etree.ElementTree import tostring

import xmlschema
from apps.ai.agent_policies import PolicyTerms, publish_agent_policy
from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.models import AIInvocation
from apps.ai.policy import OrganizationPolicy
from apps.ai.revenue_cycle import collect_revenue_cycle, request_revenue_cycle
from apps.core.integration import execute_operation
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import RoleGrant, UserClinicRole
from apps.insurance.tiss import NS, protocol_hash
from apps.providers.lifecycle import VersionInput, propose_version
from django.utils import timezone

from ai.support import model, seed
from billing.journal_support import finance_scope
from identity.permission_support import owner_context, permission_context
from insurance.cycle_support import build_cycle
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path
    from uuid import UUID

    import pytest
    from apps.ai.revenue_cycle import RevenueReview

    from billing.journal_support import FinanceSetup
    from insurance.cycle_support import Cycle
    from rbac_fixtures import RbacGraph

# SC-15 sentinels planted in protected TISS sources; a TISS card holds 20 chars.
CARD_SENTINEL: Final = "SINTETICO-SENTINELA"
PROFESSIONAL_SENTINEL: Final = "SINTETICO-SENTINELA-60-PROFISSIONAL"
PAYER_SENTINEL: Final = "SINTETICO-SENTINELA-60-OPERADORA"


@dataclass(frozen=True, slots=True)
class RevenueWorld:
    cycle: Cycle
    policy_id: UUID
    administrator: UUID

    @property
    def setup(self) -> FinanceSetup:
        return self.cycle.finance


def revenue_world(
    setup: FinanceSetup, monkeypatch: pytest.MonkeyPatch, secret_root: Path
) -> RevenueWorld:
    cycle = build_cycle(
        setup,
        membership_number=CARD_SENTINEL,
        professional_name=PROFESSIONAL_SENTINEL,
    )
    graph = setup.graph
    gateway = seed(graph, monkeypatch)
    secret = secret_root / "agent-attestation-v1.secret"
    secret.write_text(secrets.token_hex(32))
    secret.chmod(0o600)
    model(
        organization=graph.organization_a,
        clinic=setup.clinic_id,
        purpose=Purpose.REVENUE_CYCLE,
    )
    with permission_context(graph, gateway.administrator):
        current = latest_configuration(setup.clinic_id)
        assert current is not None
        set_organization_policy(
            clinic_id=setup.clinic_id,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=(Purpose.REVENUE_CYCLE.value,),
                monthly_budget_micros=10_000_000,
                purpose_budgets=((Purpose.REVENUE_CYCLE.value, 10_000_000),),
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
    # Publishing any AI-06 version needs the publisher's Finance automation.
    add_finance_role(graph, gateway.administrator, setup.clinic_id)
    with permission_context(graph, gateway.administrator):
        policy = publish_agent_policy(
            clinic_id=setup.clinic_id,
            expected_version=0,
            terms=PolicyTerms(
                "revenue_cycle",
                "A1",
                ("appeal.draft", "discrepancy.explain"),
                enabled=True,
            ),
        )
    return RevenueWorld(cycle, policy.pk, gateway.administrator)


def revenue_scope(
    world: RevenueWorld, actor: UUID | None
) -> AbstractContextManager[None]:
    """The requester's Finance scope, or another actor's exact clinic scope."""
    if actor is None:
        return finance_scope(world.setup)
    return permission_context(world.setup.graph, actor)


def request_and_collect(
    world: RevenueWorld, *, actor: UUID | None = None
) -> RevenueReview:
    with revenue_scope(world, actor):
        review = request_revenue_cycle(
            clinic_id=world.setup.clinic_id, guide_id=world.cycle.guide.pk
        )
        assert review.invocation_id is not None
        operation_id = AIInvocation.objects.get(pk=review.invocation_id).operation_id
    assert operation_id is not None
    with runtime_role():
        execute_operation(operation_id)
    with revenue_scope(world, actor):
        return collect_revenue_cycle(
            clinic_id=world.setup.clinic_id, invocation_id=review.invocation_id
        )


def add_finance_role(graph: RbacGraph, user: UUID, clinic: UUID) -> None:
    """Give an existing staff member the Finance bundle in one clinic."""
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            user_id=user,
            role=UserClinicRole.Role.FINANCE,
        )


def add_clinic_manager_role(setup: FinanceSetup) -> None:
    """The requester also manages this clinic, so the generic agent queue opens."""
    with owner_context(setup.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=setup.graph.organization_a,
            clinic_id=setup.clinic_id,
            user_id=setup.graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_MANAGER,
        )


def withdraw_finance_automation(
    setup: FinanceSetup, *, bundle_version: int | None = None
) -> None:
    """Owner-provisioned subtraction of automation.finance from Finance here.

    A version-2 subtraction leaves version-1 Finance memberships untouched.
    """
    with owner_context(setup.graph.organization_a):
        versions = (
            {bundle_version}
            if bundle_version is not None
            else set(
                UserClinicRole.objects.filter(
                    clinic_id=setup.clinic_id,
                    role=UserClinicRole.Role.FINANCE,
                    revoked_at__isnull=True,
                ).values_list("workflow_bundle_version", flat=True)
            )
        )
        for version in versions:
            RoleGrant.objects.create(
                organization_id=setup.graph.organization_a,
                clinic_id=setup.clinic_id,
                role=UserClinicRole.Role.FINANCE,
                permission="automation.finance",
                bundle_version=version,
                valid_from=timezone.now(),
            )


def changed_return(
    world: RevenueWorld, *, injection: str = "", denied: bool = False
) -> bytes:
    """Keep XSD/hash validity while changing genuine untrusted source fields."""
    root = xmlschema.XMLResource(
        world.cycle.original_return, allow="none", defuse="always"
    ).root
    if injection:
        description = root.find(f".//{{{NS}}}descricaoProcedimento")
        assert description is not None
        description.text = injection
    if denied:
        for element in root.iter():
            if element.text == "83.45":
                element.text = "73.45"
            elif element.text == "40.00":
                element.text = "50.00"
    checksum = root.find(f"{{{NS}}}epilogo/{{{NS}}}hash")
    assert checksum is not None
    checksum.text = protocol_hash(root)
    return cast("bytes", tostring(root, encoding="utf-8", xml_declaration=True))
