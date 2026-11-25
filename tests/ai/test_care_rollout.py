"""Retained clinical and administrative care proposals keep original ancestry."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4, uuid5

import pytest
from apps.ai.action_contracts import ActionAccessDeniedError, ActionRefusedError
from apps.ai.action_payload import parse_payload
from apps.ai.care_coordination import CareAccessDeniedError, _payload, decide_care_item
from apps.ai.care_models import CareProposal
from apps.ai.contracts import GatewayRefusedError, Purpose
from apps.ai.models import AgentRun
from apps.ai.pending_access import workflow
from apps.ai.pending_outputs import rows as pending_rows
from apps.ai.proposals import propose_action
from apps.workflows.care.models import CareAction
from apps.workflows.care.services import CareActionSpec, add_care_action
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from ai.care_support import acting, care_world, request_and_collect
from ai.pending_observation import observe
from ai.retained_rollout_support import (
    INVALIDATIONS,
    Invalidation,
    authorize_native,
    invalidate,
)
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def native_accept(item: CareProposal, *, clinic_id: UUID) -> None:
    """Write through care's ordinary human command and its native AI provenance."""
    authorize_native(
        invocation_id=item.invocation_id,
        clinic_id=clinic_id,
        purpose=Purpose.CARE_COORDINATION,
    )
    assert item.window_start is not None
    assert item.window_end is not None
    action = add_care_action(
        clinic_id=item.clinic_id,
        plan_id=item.plan_id,
        spec=CareActionSpec(
            kind=item.kind,
            window_start=item.window_start,
            window_end=item.window_end,
        ),
        idempotency_key=uuid5(item.pk, "care-action"),
    )
    CareProposal.objects.filter(pk=item.pk).update(
        state="accepted",
        care_action=action,
        decided_by_id=item.requested_by_id,
        decided_at=timezone.now(),
        revision=item.revision + 1,
    )


def native_reproposal(
    template: AgentRun, invocation_id: UUID, *, clinic_id: UUID
) -> AgentRun:
    """Insert a fresh native agent run while retaining the original invocation."""
    authorize_native(
        invocation_id=invocation_id,
        clinic_id=clinic_id,
        purpose=Purpose.CARE_COORDINATION,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_rollout_admission(%s,%s,false)",
            [template.configuration_id, template.clinic_id],
        )
        approval = cursor.fetchone()
    assert approval is not None
    return pending_rows(AgentRun).create(
        organization_id=template.organization_id,
        clinic_id=template.clinic_id,
        policy_id=template.policy_id,
        user_id=template.user_id,
        principal_id=template.principal_id,
        configuration_id=template.configuration_id,
        rollout_approval_id=approval[0],
        invocation_id=invocation_id,
        prompt_sha256=template.prompt_sha256,
        trigger=template.trigger,
        contract=template.contract,
        evaluation_set=template.evaluation_set,
    )


@pytest.mark.parametrize("mode", INVALIDATIONS)
@pytest.mark.parametrize("native", [False, True], ids=["python", "native"])
def test_retained_clinical_care_cannot_apply_after_rollout_invalidation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    mode: Invalidation,
    native: bool,
) -> None:
    world = care_world(rbac_graph, monkeypatch, synthetic_secret_backend)
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    with acting(rbac_graph, world.physician):
        item = CareProposal.objects.get(
            invocation_id=bundle.invocation_id, audience="clinical"
        )
        before = CareAction.objects.filter(plan=world.plan).count()
        with transaction.atomic():
            if native:
                native_accept(item, clinic_id=rbac_graph.clinic_a)
            else:
                accepted = decide_care_item(
                    clinic_id=item.clinic_id,
                    item_id=item.pk,
                    decision="accept",
                    expected_revision=item.revision,
                )
                assert (
                    observe(accepted, rbac_graph.clinic_a, lambda r: r.state)
                    == "accepted"
                )
            assert CareAction.objects.filter(plan=world.plan).count() == before + 1
            transaction.set_rollback(True)
    clinic_id = invalidate(rbac_graph, bundle.invocation_id, mode, monkeypatch)
    with acting(rbac_graph, world.physician):
        refused = (
            (DatabaseError,)
            if native
            else (
                (CareAccessDeniedError,)
                if mode == "foreign_clinic"
                else (GatewayRefusedError,)
            )
        )
        attempt = (
            partial(native_accept, item, clinic_id=clinic_id)
            if native
            else partial(
                decide_care_item,
                clinic_id=clinic_id,
                item_id=item.pk,
                decision="accept",
                expected_revision=item.revision,
            )
        )
        with pytest.raises(refused), transaction.atomic():
            attempt()
    with owner_context(rbac_graph.organization_a):
        item.refresh_from_db()
        assert item.state == "pending"
        assert item.care_action_id is None
        assert CareAction.objects.filter(plan=world.plan).count() == before


@pytest.mark.parametrize("mode", INVALIDATIONS)
@pytest.mark.parametrize("native", [False, True], ids=["python", "native"])
def test_retained_administrative_care_cannot_repropose_under_new_permission(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    mode: Invalidation,
    native: bool,
) -> None:
    world = care_world(rbac_graph, monkeypatch, synthetic_secret_backend)
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    with acting(rbac_graph, world.manager):
        item = CareProposal.objects.get(
            invocation_id=bundle.invocation_id, kind="staff_coordination"
        )
        payload = parse_payload(
            _payload(item=item, policy=world.policy, attempt=uuid4())
        )
        with transaction.atomic():
            proposal = propose_action(clinic_id=rbac_graph.clinic_a, payload=payload)
            state, run_id = observe(
                proposal, rbac_graph.clinic_a, lambda r: (r.state, r.run_id)
            )
            assert state == "validated"
            with workflow(rbac_graph.clinic_a):
                template = pending_rows(AgentRun).get(pk=run_id)
                if native:
                    assert native_reproposal(
                        template, bundle.invocation_id, clinic_id=rbac_graph.clinic_a
                    ).pk
            transaction.set_rollback(True)
    clinic_id = invalidate(rbac_graph, bundle.invocation_id, mode, monkeypatch)
    with acting(rbac_graph, world.manager):
        refused = (
            (DatabaseError,)
            if native
            else (
                (ActionAccessDeniedError,)
                if mode == "foreign_clinic"
                else (GatewayRefusedError, ActionRefusedError)
            )
        )
        attempt = (
            partial(
                native_reproposal, template, bundle.invocation_id, clinic_id=clinic_id
            )
            if native
            else partial(propose_action, clinic_id=clinic_id, payload=payload)
        )
        with pytest.raises(refused), transaction.atomic():
            attempt()
    with owner_context(rbac_graph.organization_a):
        assert not AgentRun.objects.filter(clinic_id=item.clinic_id).exists()
        item.refresh_from_db()
        assert item.state == "pending"
