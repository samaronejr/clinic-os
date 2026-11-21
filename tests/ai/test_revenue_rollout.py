"""Retained Finance output cannot borrow a successor's proposal permission."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.ai.action_contracts import ActionAccessDeniedError, ActionRefusedError
from apps.ai.action_payload import ActionPayload, parse_payload
from apps.ai.care_coordination import _payload
from apps.ai.care_models import CareProposal
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.models import ActionProposal, AgentRun
from apps.ai.proposals import propose_action
from apps.core.secrets import secret_store
from apps.tenancy.envelope import KEK_SECRET_NAME
from django.db import DatabaseError, connection, transaction

from ai.care_support import acting, care_world
from ai.care_support import request_and_collect as collect_care
from ai.retained_rollout_support import (
    INVALIDATIONS,
    Invalidation,
    authorize_native,
    invalidate,
)
from ai.revenue_support import RevenueWorld, request_and_collect
from ai.test_revenue_cycle import finance_setup, proposal_for, world
from billing.journal_support import finance_scope

if TYPE_CHECKING:
    from pathlib import Path

    from apps.ai.revenue_cycle import RevenueReview

    from rbac_fixtures import RbacGraph

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)
PYTHON_REFUSALS = (
    GatewayAccessDeniedError,
    GatewayRefusedError,
    ActionAccessDeniedError,
    ActionRefusedError,
)


def fresh_payload(template: ActionProposal) -> ActionPayload:
    data = json.loads(template.payload)
    data.update(action_id=str(uuid4()), idempotency_key=str(uuid4()))
    return parse_payload(json.dumps(data))


def native_reproposal(
    template: ActionProposal,
    payload: ActionPayload,
    *,
    invocation_id: UUID | None,
    clinic_id: UUID,
) -> ActionProposal:
    """Use real runtime DML and source facts, bypassing Python admission."""
    original = template.run
    if invocation_id is not None:
        authorize_native(
            invocation_id=invocation_id,
            clinic_id=clinic_id,
            purpose=Purpose(original.policy.capability),
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_rollout_admission(%s,%s,false)",
            [original.configuration_id, clinic_id],
        )
        permission = cursor.fetchone()[0]
    run = AgentRun.objects.create(
        organization_id=original.organization_id,
        clinic_id=clinic_id,
        policy_id=original.policy_id,
        user_id=original.user_id,
        principal_id=original.principal_id,
        configuration_id=original.configuration_id,
        invocation_id=invocation_id,
        rollout_approval_id=permission,
        prompt_sha256=original.prompt_sha256,
        trigger=original.trigger,
        contract=original.contract,
        evaluation_set=original.evaluation_set,
    )
    data = payload.data()
    proposal = ActionProposal.objects.create(
        id=UUID(str(data["action_id"])),
        organization_id=run.organization_id,
        clinic_id=run.clinic_id,
        run=run,
        payload=payload.canonical.decode(),
        digest=payload.digest,
        idempotency_key=UUID(str(data["idempotency_key"])),
        quorum=template.quorum,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_authorize_retained_proposal(%s,%s,%s)",
            [proposal.pk, clinic_id, secret_store().get_secret(KEK_SECRET_NAME)],
        )
        # The transition itself independently rejects a missing/invalid receipt.
    proposal.state = "validated"
    proposal.save(update_fields=("state",))
    return proposal


def retained_template(world: RevenueWorld) -> tuple[RevenueReview, ActionProposal]:
    review = request_and_collect(world)
    with finance_scope(world.setup):
        return review, proposal_for(world, review)


@pytest.mark.parametrize("mode", INVALIDATIONS)
@pytest.mark.parametrize("native", [False, True])
def test_retained_revenue_reproposal_keeps_original_permission(
    world: RevenueWorld,
    monkeypatch: pytest.MonkeyPatch,
    mode: Invalidation,
    native: bool,
) -> None:
    review, template = retained_template(world)
    assert review.invocation_id is not None
    with finance_scope(world.setup):
        payload = fresh_payload(template)
        before = (AgentRun.objects.count(), ActionProposal.objects.count())
        with transaction.atomic():
            allowed = (
                native_reproposal(
                    template,
                    payload,
                    invocation_id=review.invocation_id,
                    clinic_id=world.setup.clinic_id,
                )
                if native
                else propose_action(clinic_id=world.setup.clinic_id, payload=payload)
            )
            assert allowed.state == "validated"
            assert allowed.quorum == 1
            transaction.set_rollback(True)
    clinic_id = invalidate(world.setup.graph, review.invocation_id, mode, monkeypatch)
    with finance_scope(world.setup):
        attempt = (
            partial(
                native_reproposal,
                template,
                payload,
                invocation_id=review.invocation_id,
                clinic_id=clinic_id,
            )
            if native
            else partial(propose_action, clinic_id=clinic_id, payload=payload)
        )
        with (
            pytest.raises((DatabaseError,) if native else PYTHON_REFUSALS),
            transaction.atomic(),
        ):
            attempt()
        assert (AgentRun.objects.count(), ActionProposal.objects.count()) == before


def test_native_care_payload_keeps_its_own_invocation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> None:
    care = care_world(rbac_graph, monkeypatch, synthetic_secret_backend)
    old = collect_care(care)
    assert old.invocation_id is not None
    with acting(rbac_graph, care.manager):
        item = CareProposal.objects.get(
            invocation_id=old.invocation_id, kind="staff_coordination"
        )
        original = propose_action(
            clinic_id=item.clinic_id,
            payload=parse_payload(
                _payload(item=item, policy=care.policy, attempt=uuid4())
            ),
        )
    invalidate(rbac_graph, old.invocation_id, "advanced_stage", monkeypatch)
    current = collect_care(care)
    assert current.invocation_id is not None
    with acting(rbac_graph, care.manager):
        item = CareProposal.objects.get(
            invocation_id=current.invocation_id, kind="staff_coordination"
        )
        template = propose_action(
            clinic_id=item.clinic_id,
            payload=parse_payload(
                _payload(item=item, policy=care.policy, attempt=uuid4())
            ),
        )
        with transaction.atomic():
            assert (
                native_reproposal(
                    template,
                    fresh_payload(template),
                    invocation_id=current.invocation_id,
                    clinic_id=item.clinic_id,
                ).state
                == "validated"
            )
            transaction.set_rollback(True)
        before = (AgentRun.objects.count(), ActionProposal.objects.count())
        with pytest.raises(DatabaseError), transaction.atomic():
            native_reproposal(
                template,
                fresh_payload(original),
                invocation_id=current.invocation_id,
                clinic_id=item.clinic_id,
            )
        assert (AgentRun.objects.count(), ActionProposal.objects.count()) == before


def test_retained_proposal_source_receipt_is_not_runtime_writable(
    world: RevenueWorld,
) -> None:
    _review, template = retained_template(world)
    with (
        finance_scope(world.setup),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        ActionProposal.objects.filter(pk=template.pk).update(source_xid="1")


def test_native_revenue_run_requires_its_original_invocation(
    world: RevenueWorld,
) -> None:
    review, template = retained_template(world)
    assert review.invocation_id is not None
    with finance_scope(world.setup):
        payload = fresh_payload(template)
        with transaction.atomic():
            assert (
                native_reproposal(
                    template,
                    payload,
                    invocation_id=review.invocation_id,
                    clinic_id=template.clinic_id,
                ).state
                == "validated"
            )
            transaction.set_rollback(True)
        with pytest.raises(DatabaseError), transaction.atomic():
            native_reproposal(
                template, payload, invocation_id=None, clinic_id=template.clinic_id
            )


@pytest.mark.parametrize("mode", ["source_drift", "advanced_stage"])
def test_retained_revenue_idempotent_preview_rechecks_source(
    world: RevenueWorld, monkeypatch: pytest.MonkeyPatch, mode: Invalidation
) -> None:
    review, template = retained_template(world)
    assert review.invocation_id is not None
    with finance_scope(world.setup):
        payload = parse_payload(template.payload)
        assert (
            propose_action(clinic_id=template.clinic_id, payload=payload).pk
            == template.pk
        )
    invalidate(world.setup.graph, review.invocation_id, mode, monkeypatch)
    with finance_scope(world.setup), pytest.raises(PYTHON_REFUSALS):
        propose_action(clinic_id=template.clinic_id, payload=payload)


def test_retained_revenue_payload_cannot_borrow_another_invocation(
    world: RevenueWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_review, old_template = retained_template(world)
    assert old_review.invocation_id is not None
    invalidate(
        world.setup.graph, old_review.invocation_id, "advanced_stage", monkeypatch
    )
    current_review, current_template = retained_template(world)
    assert current_review.invocation_id is not None
    assert current_review.invocation_id != old_review.invocation_id
    with finance_scope(world.setup):
        before = (AgentRun.objects.count(), ActionProposal.objects.count())
        payload = fresh_payload(current_template)
        with transaction.atomic():
            assert (
                native_reproposal(
                    current_template,
                    payload,
                    invocation_id=current_review.invocation_id,
                    clinic_id=world.setup.clinic_id,
                ).state
                == "validated"
            )
            transaction.set_rollback(True)
        stale_payload = fresh_payload(old_template)
        with pytest.raises(DatabaseError), transaction.atomic():
            native_reproposal(
                current_template,
                stale_payload,
                invocation_id=current_review.invocation_id,
                clinic_id=world.setup.clinic_id,
            )
        assert (AgentRun.objects.count(), ActionProposal.objects.count()) == before
