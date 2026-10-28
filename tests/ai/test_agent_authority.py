"""Real-database authority snapshots across supported mixed bundle versions."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID

import pytest
from apps.ai.action_contracts import Refusal
from apps.ai.action_payload import parse_payload
from apps.ai.action_validation import authority_snapshot
from apps.ai.models import ActionReceipt, Approval
from apps.comms import agent_reminders
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.utils import timezone

from ai.agent_support import approve, propose
from ai.test_agent_execution import agent_password, execute, machine_login, world
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from ai.agent_support import AgentWorld

__all__ = ("agent_password", "machine_login", "world")
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


def mixed_memberships(
    subject: AgentWorld, *, primary_role: str = "owner", opposing: bool = True
) -> tuple[UUID, UUID]:
    primary_id = UUID("10000000-0000-4000-8000-000000000001")
    legacy_id = UUID("20000000-0000-4000-8000-000000000002")
    if not opposing:
        primary_id, legacy_id = legacy_id, primary_id
    graph = subject.gateway.graph
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(
            user_id=subject.gateway.administrator, clinic_id=graph.clinic_a
        ).update(revoked_at=timezone.now())
        for identifier, role, version in (
            (primary_id, primary_role, 2),
            (legacy_id, "org_admin", 1),
        ):
            UserClinicRole.objects.create(
                id=identifier,
                user_id=subject.gateway.administrator,
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                role=role,
                workflow_bundle_version=version,
            )
    return primary_id, legacy_id


def with_current_authority(subject: AgentWorld) -> AgentWorld:
    graph = subject.gateway.graph
    with (
        runtime_role(),
        tenant_context(subject.gateway.administrator, graph.organization_a),
    ):
        snapshot = authority_snapshot(
            clinic_id=graph.clinic_a, permission="automation.organization"
        )
    data = subject.payload.data()
    data["approver_authority_snapshots"] = [snapshot]
    return replace(subject, payload=parse_payload(json.dumps(data)))


@pytest.mark.parametrize("opposing", [True, False], ids=["opposed", "aligned"])
def test_sql_authority_agrees_with_canonical_payload_for_mixed_bundles(
    world: AgentWorld, opposing: bool
) -> None:
    # Given the supported clinic_admin-v2/org_admin-v1 combination.
    primary_id, legacy_id = mixed_memberships(
        world, primary_role="clinic_admin", opposing=opposing
    )
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        # When SQL produces the current authority consumed by payload admission.
        snapshot = authority_snapshot(
            clinic_id=graph.clinic_a, permission="tasks.assign"
        )

    # Then UUID order cannot override canonical bundle/id/role byte order.
    assert snapshot["memberships"] == [
        {"bundle_version": 1, "id": str(legacy_id), "role": "org_admin"},
        {"bundle_version": 2, "id": str(primary_id), "role": "clinic_admin"},
    ]
    data = world.payload.data()
    data["approver_authority_snapshots"] = [snapshot]
    admitted = parse_payload(json.dumps(data))
    assert admitted.data()["approver_authority_snapshots"] == [snapshot]


def test_mixed_bundle_authority_can_approve_and_execute(world: AgentWorld) -> None:
    # Given current mixed memberships whose UUID order opposes canonical order.
    mixed_memberships(world)
    subject = with_current_authority(world)
    proposal = propose(subject)
    approval = approve(subject, proposal)

    # When the authenticated machine executes that exact approved effect.
    result = execute(subject, proposal.pk)

    # Then current SQL authority and the attested payload authorize one receipt.
    assert result.state == "receipt"
    assert result.receipt_id is not None
    snapshots = subject.payload.data()["approver_authority_snapshots"]
    assert isinstance(snapshots, list)
    graph = subject.gateway.graph
    with (
        runtime_role(),
        tenant_context(subject.gateway.administrator, graph.organization_a),
    ):
        assert Approval.objects.get(pk=approval.pk).authority_snapshot == snapshots[0]
        assert ActionReceipt.objects.get(pk=result.receipt_id).digest == proposal.digest


def test_revoked_mixed_membership_refuses_the_approved_effect(
    world: AgentWorld,
) -> None:
    # Given a mixed-membership approval followed by revocation of its v2 grant.
    primary_id, _legacy_id = mixed_memberships(world)
    subject = with_current_authority(world)
    proposal = propose(subject)
    approve(subject, proposal)
    graph = subject.gateway.graph
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(pk=primary_id).update(revoked_at=timezone.now())

    # When execution rechecks the stored approval against current authority.
    with patch.object(
        agent_reminders, "send_reminder", wraps=agent_reminders.send_reminder
    ) as provider:
        result = execute(subject, proposal.pk)

    # Then the old approval cannot cause an effect or acquire a receipt.
    assert result.reason is Refusal.AUTHORITY
    assert result.receipt_id is None
    provider.assert_not_called()
