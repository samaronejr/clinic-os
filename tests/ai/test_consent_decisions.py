"""Real Finance decisions: a displayed digest is not consent to vote or act."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from apps.ai.action_contracts import ActionAccessDeniedError
from apps.ai.approvals import reject_action
from apps.ai.execution import execute_action
from apps.ai.models import ActionExecution, ActionProposal, Approval
from apps.consent.services import record_ai_disclosure, revoke_consent
from apps.intake.patient_access import patient_session_context
from django.db import connection, connections, transaction

from ai.chart_consent_support import accept_ai
from ai.pending_display_support import withdraw
from ai.revenue_support import request_and_collect
from ai.test_pending_render_commit import commit_between_load_and_render
from ai.test_revenue_cycle import approve, finance_setup, proposal_for, world
from auth.stepup_test_support import verified_request
from billing.journal_support import finance_scope
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from django.http import HttpRequest

    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)


def _decide(
    command: str,
    world: RevenueWorld,
    proposal: ActionProposal,
    evidence: HttpRequest,
) -> None:
    if command == "approve":
        approve(world, proposal, evidence)
    elif command == "reject":
        reject_action(
            clinic_id=world.setup.clinic_id,
            proposal_id=proposal.pk,
            expected_digest=proposal.digest,
        )
    else:
        assert command == "execute"
        result = execute_action(
            clinic_id=world.setup.clinic_id, proposal_id=proposal.pk
        )
        assert result.state == "receipt"


def _state(world: RevenueWorld, pk: UUID) -> tuple[str, int, int]:
    """Inspect durable state, including rows no longer visible to the reviewer."""
    with owner_context(world.setup.graph.organization_a):
        return (
            ActionProposal.objects.get(pk=pk).state,
            Approval.objects.filter(proposal_id=pk).count(),
            ActionExecution.objects.filter(proposal_id=pk).count(),
        )


def _prepared(
    world: RevenueWorld, command: str
) -> tuple[UUID, UUID, ActionProposal, HttpRequest]:
    setup = world.setup
    patient, receipt = accept_ai(setup.graph, setup.appointment.enrollment_id)
    review = request_and_collect(world)
    evidence = verified_request(setup.graph.clinic_admin, persisted=True)
    with finance_scope(setup):
        proposal = proposal_for(world, review)
    # Assert the durable result in a fresh reviewer transaction, not via a
    # retained command instance while the proposal's consent fence is held.
    with finance_scope(setup):
        proposal = ActionProposal.objects.get(pk=proposal.pk)
        assert proposal.state == "validated"
        if command == "execute":
            approve(world, proposal, evidence)
    return patient, receipt, proposal, evidence


@pytest.mark.parametrize("command", ["approve", "reject", "execute"])
def test_choice_committed_during_decision_load_is_a_scoped_refusal(
    world: RevenueWorld, command: str
) -> None:
    patient, receipt, proposal, evidence = _prepared(world, command)
    before = _state(world, proposal.pk)
    loaded = ActionProposal.from_db
    entered: list[bool] = []
    with commit_between_load_and_render(lambda: withdraw(patient, receipt)) as fence:

        def after_load(
            db: str, field_names: list[str], values: Sequence[object]
        ) -> ActionProposal:
            row = loaded(db, field_names, values)
            if row.pk == proposal.pk and not entered:
                entered.append(connection.in_atomic_block)
                fence(None)
            return row

        with (
            finance_scope(world.setup),
            patch.object(ActionProposal, "from_db", side_effect=after_load),
            pytest.raises(ActionAccessDeniedError),
        ):
            _decide(command, world, proposal, evidence)
    assert entered == [True]
    with finance_scope(world.setup):
        assert not ActionProposal.objects.filter(pk=proposal.pk).exists()
    assert _state(world, proposal.pk) == before


def _writer_can_enter(clinic_id: UUID) -> bool:
    """Non-mutating witness on a distinct backend; no consent row or vote is written."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                [f"ai-pending-decision:{clinic_id}"],
            )
            result = cursor.fetchone()
            return result is not None and result[0] is True
    finally:
        connections.close_all()


@pytest.mark.parametrize("command", ["approve", "reject", "execute"])
def test_decision_keeps_the_consent_fence_to_commit(
    world: RevenueWorld, command: str
) -> None:
    patient, receipt, proposal, evidence = _prepared(world, command)
    before = _state(world, proposal.pk)
    with finance_scope(world.setup), transaction.atomic():
        _decide(command, world, proposal, evidence)
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer_entered = pool.submit(
                _writer_can_enter, world.setup.clinic_id
            ).result()
        transaction.set_rollback(True)
    assert _state(world, proposal.pk) == before
    withdraw(patient, receipt)
    with finance_scope(world.setup), pytest.raises(ActionAccessDeniedError):
        _decide(command, world, proposal, evidence)
    assert _state(world, proposal.pk) == before
    assert not writer_entered, (
        "a consent writer could enter before the decision committed"
    )


def _another_decision_could_enter(clinic_id: UUID) -> bool:
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock_shared(hashtextextended(%s,0))",
                [f"ai-pending-decision:{clinic_id}"],
            )
            result = cursor.fetchone()
            return result is not None and result[0] is True
    finally:
        connections.close_all()


@pytest.mark.parametrize("choice", ["patient", "visit"])
def test_real_choice_writer_excludes_new_decisions_until_commit(
    world: RevenueWorld, choice: str
) -> None:
    patient, receipt, proposal, _request = _prepared(world, "reject")
    setup = world.setup
    with ThreadPoolExecutor(max_workers=1) as pool:
        if choice == "patient":
            with runtime_role(), patient_session_context(patient):
                revoke_consent(acceptance_id=receipt)
                admitted = pool.submit(
                    _another_decision_could_enter, setup.clinic_id
                ).result()
        else:
            with as_actor(setup.graph, setup.graph.physician):
                record_ai_disclosure(
                    clinic_id=setup.clinic_id,
                    encounter_id=world.cycle.guide.encounter_id,
                    informed=True,
                    refused=True,
                )
                admitted = pool.submit(
                    _another_decision_could_enter, setup.clinic_id
                ).result()
        assert pool.submit(_another_decision_could_enter, setup.clinic_id).result()
    with finance_scope(setup):
        assert not ActionProposal.objects.filter(pk=proposal.pk).exists()
    assert not admitted, "new decision could overtake an uncommitted choice"
