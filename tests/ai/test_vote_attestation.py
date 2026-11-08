"""ai.0012: PostgreSQL itself refuses forged votes and approvals without them.

Each oracle acts as clinic_app with an entitled approver's own actor and tenant
settings and raw SQL, so no Python guard is on the path. It runs for a
non-revenue A3 reminder and an AI-06 A1 appeal draft, both quorum 1. On ai.0011
the forged insert is accepted, a vote without verified step-up counts as
current and a raw validated->approved update moves the proposal.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.attestations import step_up_evidence
from apps.ai.execution import execute_action
from apps.ai.models import ActionExecution, ActionProposal, Approval
from apps.identity.session_inventory import register_session, session_hash
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from psycopg.errors import InsufficientPrivilege

from ai.agent_support import AgentWorld, agent_world, propose
from ai.agent_support import approve as approve_agent
from ai.revenue_support import RevenueWorld, request_and_collect, revenue_world
from ai.test_revenue_cycle import approve as approve_revenue
from ai.test_revenue_cycle import proposal_for
from auth.stepup_test_support import verified_request
from billing.journal_support import finance_scope, finance_setup
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path
    from uuid import UUID

    from apps.ai.attestations import StepUpEvidence
    from django.db.backends.utils import CursorWrapper
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)

FORGED_VOTE = """
INSERT INTO clinic_app.ai_approval(id,created_at,organization_id,clinic_id,
 proposal_id,approver_id,digest,authority_snapshot,step_up_evidence_ref,
 step_up_evidence,key_id,attestation)
VALUES(%s,statement_timestamp(),%s,%s,%s,%s,%s,%s::jsonb,%s,'{}'::jsonb,
 'agent-attestation-v1',repeat('0',64))
"""
RAW_EXECUTION = """
INSERT INTO clinic_app.ai_actionexecution(id,created_at,organization_id,clinic_id,
 proposal_id,digest,fencing_token,provider_key,state,intent_at,send_started_at,
 deadline,reserved_micros,reason_code)
VALUES(%s,statement_timestamp(),%s,%s,%s,%s,1,%s,'executing',statement_timestamp(),
 NULL,statement_timestamp()+interval '30 seconds',1,'')
"""
RECORD_VOTE = (
    "SELECT clinic_app.agent_record_approval(%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s)"
)
RAW_APPROVAL = "UPDATE clinic_app.ai_actionproposal SET state=%s WHERE id=%s"
QUORUM_REFUSED = "agent approval quorum refused"
# Owner tampering that switches off only the agent guard of one table.
GUARD_TOGGLES = {
    table: (
        f"ALTER TABLE clinic_app.{table} DISABLE TRIGGER agent_record_guard",
        f"ALTER TABLE clinic_app.{table} ENABLE TRIGGER agent_record_guard",
    )
    for table in ("ai_approval", "ai_actionproposal", "ai_actionexecution")
}


@dataclass(frozen=True, slots=True)
class Subject:
    """One validated quorum-1 proposal and the approver entitled to vote on it."""

    kind: str
    graph: RbacGraph
    organization_id: UUID
    clinic_id: UUID
    approver: UUID
    permission: str
    proposal: ActionProposal
    world: AgentWorld | RevenueWorld


@pytest.fixture(params=["agent", "revenue"])
def subject(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    settings: SettingsWrapper,
) -> Subject:
    if request.param == "agent":
        graph: RbacGraph = request.getfixturevalue("rbac_graph")
        world = agent_world(graph, monkeypatch, synthetic_secret_backend)
        proposal = propose(world)
        found = Subject(
            "agent",
            graph,
            graph.organization_a,
            graph.clinic_a,
            world.gateway.administrator,
            "automation.organization",
            proposal,
            world,
        )
    else:
        settings.INSURANCE_SYNTHETIC_ENABLED = True
        setup = request.getfixturevalue("finance_setup")
        revenue = revenue_world(setup, monkeypatch, synthetic_secret_backend)
        review = request_and_collect(revenue)
        with finance_scope(setup):
            proposal = proposal_for(revenue, review)
        found = Subject(
            "revenue",
            setup.graph,
            setup.graph.organization_a,
            setup.clinic_id,
            setup.graph.clinic_admin,
            "tiss.manage",
            proposal,
            revenue,
        )
    assert found.proposal.state == "validated"
    return found


@contextmanager
def acting(subject: Subject, actor: UUID | None = None) -> Iterator[CursorWrapper]:
    """Raw SQL as clinic_app with only the actor and tenant settings bound."""
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [
                str(subject.organization_id),
                str(subject.approver if actor is None else actor),
            ],
        )
        yield cursor


def snapshot(subject: Subject) -> str:
    """The approver's real, current authority snapshot, as the vote guard wants."""
    with acting(subject) as cursor:
        cursor.execute(
            "SELECT clinic_app.agent_snapshot(%s,%s)",
            [subject.clinic_id, subject.permission],
        )
        row = cursor.fetchone()
    assert row is not None
    assert isinstance(row[0], str)
    return row[0]


def forged_values(subject: Subject, vote: UUID, current: str) -> list[UUID | str]:
    return [
        vote,
        subject.organization_id,
        subject.clinic_id,
        subject.proposal.pk,
        subject.approver,
        subject.proposal.digest,
        current,
        uuid4(),
    ]


@contextmanager
def unguarded(subject: Subject, table: str) -> Iterator[CursorWrapper]:
    """Owner tampering past one agent guard; every other constraint still holds."""
    disable, enable = GUARD_TOGGLES[table]
    with owner_context(subject.organization_id), connection.cursor() as cursor:
        cursor.execute(disable)
        try:
            yield cursor
        finally:
            # Deferred key checks run now, so the guard can be switched back on.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(enable)


def unverified_vote(subject: Subject) -> UUID:
    """A vote that bypassed the vote writer, as one recorded before ai.0012."""
    current = snapshot(subject)
    vote = uuid4()
    # The snapshot is the approver's genuinely current one.
    with unguarded(subject, "ai_approval") as cursor:
        cursor.execute(FORGED_VOTE, forged_values(subject, vote, current))
    return vote


def force_proposal(subject: Subject, column: str, value: str | None) -> None:
    """Move the state machine, or erase the quorum, without the guard."""
    statement = {
        "state": "UPDATE clinic_app.ai_actionproposal SET state=%s WHERE id=%s",
        "quorum": "UPDATE clinic_app.ai_actionproposal SET quorum=%s WHERE id=%s",
    }[column]
    with unguarded(subject, "ai_actionproposal") as cursor:
        cursor.execute(statement, [value, subject.proposal.pk])


def force_execution(subject: Subject) -> UUID:
    """Claim an execution without the guard."""
    execution = uuid4()
    with unguarded(subject, "ai_actionexecution") as cursor:
        cursor.execute(RAW_EXECUTION, execution_values(subject, execution))
    return execution


def execution_values(subject: Subject, execution: UUID) -> list[UUID | str]:
    return [
        execution,
        subject.organization_id,
        subject.clinic_id,
        subject.proposal.pk,
        subject.proposal.digest,
        uuid4(),
    ]


def state_of(subject: Subject) -> str:
    with owner_context(subject.organization_id):
        return ActionProposal.objects.get(pk=subject.proposal.pk).state


def votes_of(subject: Subject) -> list[UUID]:
    with owner_context(subject.organization_id):
        return list(
            Approval.objects.filter(proposal=subject.proposal).values_list(
                "pk", flat=True
            )
        )


def approve_legitimately(subject: Subject) -> Approval:
    """The unchanged Python path: fresh TOTP step-up, then approve_action."""
    if isinstance(subject.world, AgentWorld):
        return approve_agent(subject.world, subject.proposal)
    request = verified_request(subject.approver, persisted=True)
    with finance_scope(subject.world.setup):
        return approve_revenue(subject.world, subject.proposal, request)


def test_sql_refuses_a_forged_vote_from_the_entitled_approver(
    subject: Subject,
) -> None:
    current = snapshot(subject)
    with acting(subject) as cursor:
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            cursor.execute(FORGED_VOTE, forged_values(subject, uuid4(), current))
        # Runtime holds no INSERT on votes: the role cannot write one at all.
        assert type(refused.value.__cause__) is InsufficientPrivilege
    assert votes_of(subject) == []
    assert state_of(subject) == "validated"


def test_sql_never_counts_a_vote_without_verified_step_up(subject: Subject) -> None:
    vote = unverified_vote(subject)
    with acting(subject) as cursor:
        cursor.execute(
            "SELECT clinic_app.agent_approval_current(%s,%s)",
            [subject.proposal.pk, vote],
        )
        assert cursor.fetchone() == (False,)


def refuse_raw_approval(subject: Subject) -> None:
    with (
        acting(subject) as cursor,
        pytest.raises(DatabaseError, match=QUORUM_REFUSED),
        transaction.atomic(),
    ):
        cursor.execute(RAW_APPROVAL, ["approved", subject.proposal.pk])
    assert state_of(subject) == "validated"


def test_sql_refuses_raw_approval_without_current_verified_votes(
    subject: Subject,
) -> None:
    # With no vote at all, then with one that never passed verified step-up.
    refuse_raw_approval(subject)
    unverified_vote(subject)
    refuse_raw_approval(subject)


def test_sql_refuses_execution_and_dispatch_of_an_approval_without_votes(
    subject: Subject,
) -> None:
    # A pre-ai.0012 approval: an unverified vote and an approved state.
    unverified_vote(subject)
    force_proposal(subject, "state", "approved")
    with acting(subject) as cursor:
        with pytest.raises(DatabaseError, match=QUORUM_REFUSED), transaction.atomic():
            cursor.execute(RAW_APPROVAL, ["executing", subject.proposal.pk])
        with pytest.raises(DatabaseError, match=QUORUM_REFUSED), transaction.atomic():
            cursor.execute(RAW_EXECUTION, execution_values(subject, uuid4()))
    # Even an execution forced past the guard cannot reach its send boundary.
    execution = force_execution(subject)
    force_proposal(subject, "state", "executing")
    with (
        acting(subject) as cursor,
        pytest.raises(DatabaseError, match=QUORUM_REFUSED),
        transaction.atomic(),
    ):
        cursor.execute(
            "UPDATE clinic_app.ai_actionexecution "
            "SET send_started_at=statement_timestamp() WHERE id=%s",
            [execution],
        )
    with owner_context(subject.organization_id):
        claimed = ActionExecution.objects.get(proposal=subject.proposal)
        assert (claimed.pk, claimed.send_started_at) == (execution, None)


def test_python_approval_still_records_one_counted_vote(subject: Subject) -> None:
    vote = approve_legitimately(subject)
    assert vote.step_up_verified_at is not None
    assert (
        vote.created_at - timedelta(minutes=5)
        < vote.step_up_verified_at
        <= vote.created_at
    )
    assert state_of(subject) == "approved"
    with acting(subject) as cursor:
        cursor.execute(
            "SELECT clinic_app.agent_approval_current(%s,%s)",
            [subject.proposal.pk, vote.pk],
        )
        assert cursor.fetchone() == (True,)
    # Runtime still cannot rewrite the recorded instant or the vote.
    with (
        acting(subject) as cursor,
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        cursor.execute(
            "UPDATE clinic_app.ai_approval SET step_up_verified_at=NULL WHERE id=%s",
            [vote.pk],
        )
    if isinstance(subject.world, RevenueWorld):
        with finance_scope(subject.world.setup):
            result = execute_action(
                clinic_id=subject.clinic_id, proposal_id=subject.proposal.pk
            )
        assert result.state == "receipt"


@dataclass(frozen=True, slots=True)
class VoteCall:
    """One direct call of the vote writer; unset fields keep the genuine facts."""

    evidence: StepUpEvidence
    snapshot: str
    actor: UUID | None = None
    session: bytes | None = None
    facts: Mapping[str, object] | None = None
    signature: str | None = None


def genuine_call(subject: Subject, actor: UUID) -> VoteCall:
    """Server-signed step-up facts for this digest from a persisted session."""
    request = verified_request(actor, persisted=True)
    with acting(subject, actor):
        evidence = step_up_evidence(
            request, clinic_id=subject.clinic_id, digest=subject.proposal.digest
        )
    return VoteCall(evidence, snapshot(subject), actor=actor)


def record_vote(subject: Subject, call: VoteCall) -> bool:
    """Call the only vote writer directly; True means it inserted a vote."""
    evidence = call.evidence
    with acting(subject, call.actor) as cursor:
        cursor.execute(
            RECORD_VOTE,
            [
                subject.proposal.pk,
                uuid4(),
                subject.proposal.digest,
                call.snapshot,
                evidence.evidence_id,
                json.dumps(
                    {
                        "facts": evidence.value() if call.facts is None else call.facts,
                        "signature": evidence.signature
                        if call.signature is None
                        else call.signature,
                    }
                ),
                "0" * 64,
                evidence.session if call.session is None else call.session,
            ],
        )
        row = cursor.fetchone()
    assert row is not None
    return bool(row[0])


def test_vote_writer_requires_this_approvers_fresh_verified_session(
    subject: Subject,
) -> None:
    genuine = genuine_call(subject, subject.approver)
    stranger = genuine_call(subject, subject.graph.physician)
    unverified = secrets.token_urlsafe(24)
    with runtime_role():
        register_session(
            user_id=subject.approver,
            session_key=unverified,
            expires_at=timezone.now() + timedelta(hours=1),
            network="",
            family="other",
        )
    evidence = genuine.evidence
    facts = evidence.value()
    for session in (b"", session_hash(unverified), stranger.evidence.session):
        assert not record_vote(subject, replace(genuine, session=session))
    for changed in (
        {},
        {**facts, "digest": "f" * 64},
        {**facts, "user_id": str(subject.graph.physician)},
        {**facts, "clinic_id": str(subject.graph.clinic_b)},
        {**facts, "verified_at": evidence.verified_at - 301},
        {**facts, "unexpected": "synthetic"},
    ):
        assert not record_vote(subject, replace(genuine, facts=changed))
    assert not record_vote(subject, replace(genuine, signature=""))
    # A verified session does not make an unauthorized caller a voter.
    assert not record_vote(subject, stranger)
    # Revoked by its own user, as a logout does.
    with acting(subject) as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_revoke_hash(%s)", [evidence.session]
        )
        assert cursor.fetchone() == (True,)
    assert not record_vote(subject, genuine)
    assert votes_of(subject) == []
    # The unchanged Python path then records exactly one counted vote.
    vote = approve_legitimately(subject)
    assert votes_of(subject) == [vote.pk]
    assert state_of(subject) == "approved"


def test_vote_writer_refuses_an_inactive_approver(subject: Subject) -> None:
    genuine = genuine_call(subject, subject.approver)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_user SET is_active=false WHERE id=%s",
            [subject.approver],
        )
    try:
        assert not record_vote(subject, genuine)
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_user SET is_active=true WHERE id=%s",
                [subject.approver],
            )
    assert votes_of(subject) == []
    # Reactivated, the approver verifies again and votes through Python.
    vote = approve_legitimately(subject)
    assert votes_of(subject) == [vote.pk]


def test_proposals_without_a_declared_quorum_never_advance(subject: Subject) -> None:
    genuine = genuine_call(subject, subject.approver)
    # A proposal recorded before ai.0012 carries no declared quorum.
    force_proposal(subject, "quorum", None)
    # Even a vote with genuinely verified step-up cannot approve it.
    assert record_vote(subject, genuine)
    refuse_raw_approval(subject)
