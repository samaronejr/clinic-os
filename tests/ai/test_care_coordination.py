"""Todo 53 (AI-05): care-coordination proposals from approved plans.

Real PostgreSQL roles, the deployed row security and guards, the governed
gateway with its deterministic fixture model, and the todo-39 agent platform.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.ai.action_contracts import ActionInputError, Refusal
from apps.ai.action_payload import parse_payload
from apps.ai.care_coordination import (
    CareAccessDeniedError,
    CareConflictError,
    approve_care_bundle,
    care_bundle,
    collect_care_coordination,
    decide_care_item,
    pending_bundle,
)
from apps.ai.care_models import CareProposal
from apps.ai.models import ActionProposal, AIArtifact
from apps.audit.models import AuditEvent
from apps.identity.models import User
from apps.workflows.care.models import CareAction
from django.db import DatabaseError, connection, connections, transaction

from ai.care_support import (
    INVALID_PLAN,
    MEDICATION_PLAN,
    MEDICATION_SENTINEL,
    CareWorld,
    acting,
    approve_bundle,
    care_world,
    request_and_collect,
)
from identity.permission_support import owner_context, permission_context
from workflows.permission_support import permission_actor
from workflows.test_careplans import amended

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
SAFE_KEYS = {
    "clinic_id",
    "http_method",
    "http_status",
    "object_verb",
    "reason_code",
    "request_id",
}


@pytest.fixture
def world(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> CareWorld:
    return care_world(rbac_graph, monkeypatch, synthetic_secret_backend)


def items_of(world: CareWorld, invocation: UUID) -> list[CareProposal]:
    with owner_context(world.graph.organization_a):
        return list(
            CareProposal.objects.filter(invocation_id=invocation).order_by("position")
        )


def actions_of(world: CareWorld) -> list[CareAction]:
    with owner_context(world.graph.organization_a):
        return list(
            CareAction.objects.filter(plan_id=world.plan.pk).order_by("created_at")
        )


def test_fixture_output_becomes_typed_proposals_citing_the_plan_version(
    world: CareWorld,
) -> None:
    bundle = request_and_collect(world)
    assert bundle.state == "ready"
    assert bundle.invocation_id is not None
    rows = items_of(world, bundle.invocation_id)
    assert [(row.kind, row.audience, row.state) for row in rows] == [
        ("return_window", "administrative", "pending"),
        ("staff_coordination", "administrative", "pending"),
        ("questionnaire", "clinical", "pending"),
    ]
    anchor = world.plan.created_at
    window = rows[0]
    assert window.window_start == anchor + timedelta(days=42)
    assert window.window_end == anchor + timedelta(days=49)
    assert all(row.plan_id == world.plan.pk for row in rows)
    assert all(row.source_version_id == world.version.pk for row in rows)
    assert all(row.plan_revision == world.plan.revision for row in rows)
    # A replay returns the same rows instead of a second bundle.
    with acting(world.graph, world.physician):
        again = collect_care_coordination(
            clinic_id=world.graph.clinic_a, invocation_id=bundle.invocation_id
        )
    assert [row.pk for row in again.items] == [row.pk for row in rows]


def test_staff_approve_the_previewed_administrative_bundle(world: CareWorld) -> None:
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    before = len(actions_of(world))
    outcomes = approve_bundle(world, bundle.invocation_id)
    assert [outcome.state for outcome in outcomes] == ["approved", "approved"]
    rows = items_of(world, bundle.invocation_id)
    approved = [row for row in rows if row.state == "approved"]
    assert {row.kind for row in approved} == {"return_window", "staff_coordination"}
    assert all(row.decided_by_id == world.manager for row in approved)
    # Each effect went through its own digest-bound A1 proposal and receipt.
    with owner_context(world.graph.organization_a):
        proposals = ActionProposal.objects.filter(
            pk__in=[row.action_proposal_id for row in approved]
        )
        assert {proposal.state for proposal in proposals} == {"receipt"}
        for proposal in proposals:
            data = parse_payload(proposal.payload).data()
            assert data["operation"] == {"name": "care_action.create", "version": 1}
            assert data["patient_id"] == str(world.plan.enrollment_id)
    actions = actions_of(world)
    assert len(actions) == before + 2
    created = {action.pk: action for action in actions}
    for row in approved:
        assert row.care_action_id is not None
        action = created[row.care_action_id]
        assert (action.kind, action.window_start, action.window_end) == (
            row.kind,
            row.window_start,
            row.window_end,
        )
        assert action.created_by_id == world.manager
        assert action.task_id is not None
    # The clinical item never joined the staff bundle.
    questionnaire = rows[2]
    assert (questionnaire.kind, questionnaire.state) == ("questionnaire", "pending")


def test_stale_proposals_are_rejected_after_the_plan_changes(world: CareWorld) -> None:
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    before = len(actions_of(world))
    # Scenario 8: the clinician amends the source note after the run.
    amended(world.graph, world.version)
    outcomes = approve_bundle(world, bundle.invocation_id)
    assert {outcome.state for outcome in outcomes} == {"stale"}
    assert {outcome.reason for outcome in outcomes} == {Refusal.SOURCE.value}
    clinical = items_of(world, bundle.invocation_id)[2]
    with acting(world.graph, world.physician):
        decided = decide_care_item(
            clinic_id=world.graph.clinic_a,
            item_id=clinical.pk,
            decision="accept",
            expected_revision=clinical.revision,
        )
    assert (decided.state, decided.reason_code) == ("stale", "source_superseded")
    assert len(actions_of(world)) == before
    assert {row.state for row in items_of(world, bundle.invocation_id)} == {"stale"}


def test_clinical_action_without_clinician_approval_is_refused(
    world: CareWorld,
) -> None:
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    clinical = items_of(world, bundle.invocation_id)[2]
    before = len(actions_of(world))
    # Staff cannot see, accept or bundle a clinical item.
    with acting(world.graph, world.manager), pytest.raises(CareAccessDeniedError):
        decide_care_item(
            clinic_id=world.graph.clinic_a,
            item_id=clinical.pk,
            decision="accept",
            expected_revision=clinical.revision,
        )
    with acting(world.graph, world.manager):
        staff_view = care_bundle(
            clinic_id=world.graph.clinic_a, invocation_id=bundle.invocation_id
        )
        assert {row.audience for row in staff_view.items} == {"administrative"}
        assert all(
            row.audience == "administrative"
            for row in pending_bundle(
                clinic_id=world.graph.clinic_a, invocation_id=bundle.invocation_id
            )
        )
    # A clinical kind is not representable in the staff command at all.
    approve_bundle(world, bundle.invocation_id)
    with owner_context(world.graph.organization_a):
        template = ActionProposal.objects.order_by("-created_at").first()
        assert template is not None
        data = json.loads(template.payload)
    data["parameters"]["kind"] = "questionnaire"
    with pytest.raises(ActionInputError):
        parse_payload(json.dumps(data))
    assert len(actions_of(world)) == before + 2
    # The patient's clinician decides it; the action is clinician-approved.
    with acting(world.graph, world.physician):
        accepted = decide_care_item(
            clinic_id=world.graph.clinic_a,
            item_id=clinical.pk,
            decision="accept",
            expected_revision=clinical.revision,
        )
    assert accepted.state == "accepted"
    assert accepted.care_action_id is not None
    with owner_context(world.graph.organization_a):
        action = CareAction.objects.get(pk=accepted.care_action_id)
    assert (action.kind, action.approved_by_id) == ("questionnaire", world.physician)


def test_validator_rejects_a_medication_change_from_the_fake_model(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> None:
    world = care_world(
        rbac_graph, monkeypatch, synthetic_secret_backend, text=MEDICATION_PLAN
    )
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    rows = items_of(world, bundle.invocation_id)
    assert [(row.kind, row.state, row.reason_code) for row in rows] == [
        ("return_window", "pending", ""),
        ("unsupported", "refused", "kind_forbidden"),
        ("referral", "refused", "kind_forbidden"),
    ]
    assert rows[1].window_start is None
    # The model's medication text exists only in the encrypted gateway output.
    with owner_context(rbac_graph.organization_a):
        audit = list(AuditEvent.objects.values_list("payload", flat=True))
        stored = list(CareProposal.objects.values())
        assert AIArtifact.objects.filter(kind="output").exists()
    assert all(set(payload) <= SAFE_KEYS for payload in audit)
    assert MEDICATION_SENTINEL not in json.dumps(audit, default=str)
    assert MEDICATION_SENTINEL not in json.dumps(stored, default=str)
    # Staff never see the clinician's refusals.
    with acting(world.graph, world.manager):
        staff_view = care_bundle(
            clinic_id=world.graph.clinic_a, invocation_id=bundle.invocation_id
        )
    assert [row.state for row in staff_view.items] == ["pending"]


def test_one_invalid_item_is_refused_while_the_others_are_approved(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> None:
    world = care_world(
        rbac_graph, monkeypatch, synthetic_secret_backend, text=INVALID_PLAN
    )
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    rows = items_of(world, bundle.invocation_id)
    assert [(row.kind, row.state, row.reason_code) for row in rows] == [
        ("return_window", "pending", ""),
        ("staff_coordination", "pending", ""),
        ("return_window", "refused", "window_invalid"),
    ]
    outcomes = approve_bundle(world, bundle.invocation_id)
    assert [outcome.state for outcome in outcomes] == ["approved", "approved"]
    assert items_of(world, bundle.invocation_id)[2].state == "refused"


def test_changed_preview_refuses_the_whole_bundle(world: CareWorld) -> None:
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    before = len(actions_of(world))
    with acting(world.graph, world.manager), pytest.raises(CareConflictError):
        approve_care_bundle(
            clinic_id=world.graph.clinic_a,
            invocation_id=bundle.invocation_id,
            digest="0" * 64,
            step_up=lambda _digest: pytest.fail("no effect before the digest check"),
        )
    assert len(actions_of(world)) == before
    # Approving twice finds nothing left to approve.
    approve_bundle(world, bundle.invocation_id)
    with pytest.raises(CareConflictError):
        approve_bundle(world, bundle.invocation_id)


def test_unknown_and_foreign_selectors_share_one_denial(world: CareWorld) -> None:
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    clinical = items_of(world, bundle.invocation_id)[2]
    graph = world.graph
    other, _enrollment = permission_actor(graph, "physician")
    denials = []
    for clinic, invocation, user in (
        (graph.clinic_a, uuid4(), world.physician),
        (graph.clinic_b, bundle.invocation_id, world.physician),
        (graph.clinic_a, bundle.invocation_id, other),
    ):
        with acting(graph, user), pytest.raises(CareAccessDeniedError) as denied:
            collect_care_coordination(clinic_id=clinic, invocation_id=invocation)
        denials.append(str(denied.value))
    for clinic, item in ((graph.clinic_a, uuid4()), (graph.clinic_b, clinical.pk)):
        with (
            acting(graph, world.physician),
            pytest.raises(CareAccessDeniedError) as denied,
        ):
            decide_care_item(
                clinic_id=clinic, item_id=item, decision="reject", expected_revision=1
            )
        denials.append(str(denied.value))
    assert set(denials) == {"care coordination access denied"}


def test_database_guards_refuse_raw_forgery(world: CareWorld) -> None:
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    rows = items_of(world, bundle.invocation_id)
    admin = rows[0]
    graph = world.graph
    # Staff cannot insert proposals; only the requesting clinician can.
    with (
        acting(graph, world.manager),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        CareProposal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            invocation_id=bundle.invocation_id,
            plan_id=world.plan.pk,
            enrollment_id=world.plan.enrollment_id,
            source_version_id=world.version.pk,
            plan_revision=world.plan.revision,
            requested_by_id=world.manager,
            position=3,
            kind="return_window",
            audience="administrative",
            state="pending",
            window_start=admin.window_start,
            window_end=admin.window_end,
        )
    # A caller cannot forge another actor's decision.
    with (
        acting(graph, world.manager),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        CareProposal.objects.filter(pk=admin.pk).update(
            state="rejected",
            decided_by_id=graph.physician,
            decided_at=admin.created_at,
            revision=admin.revision + 1,
        )
    with (
        acting(graph, world.physician),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        CareProposal.objects.filter(pk=admin.pk).delete()
    assert items_of(world, bundle.invocation_id)[0].state == "pending"


def _forged_row(template: CareProposal, actor: UUID) -> CareProposal:
    return CareProposal(
        organization_id=template.organization_id,
        clinic_id=template.clinic_id,
        invocation_id=template.invocation_id,
        plan_id=template.plan_id,
        enrollment_id=template.enrollment_id,
        source_version_id=template.source_version_id,
        plan_revision=template.plan_revision,
        requested_by_id=actor,
        position=3,
        kind="staff_coordination",
        audience="administrative",
        state="pending",
        window_start=template.window_start,
        window_end=template.window_end,
    )


def test_care_sql_rechecks_inactive_and_foreign_actor(world: CareWorld) -> None:
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    graph = world.graph
    pending = items_of(world, bundle.invocation_id)[0]
    foreign, _enrollment = permission_actor(graph, "physician")
    with owner_context(graph.organization_a):
        User.objects.filter(pk=world.physician).update(is_active=False)
    try:
        # Raw actor GUCs only: no Python guard runs before the database decides.
        for actor in (world.physician, foreign):
            forged = _forged_row(pending, actor)
            with pytest.raises(DatabaseError), permission_context(graph, actor):
                forged.save(force_insert=True)
            with permission_context(graph, actor):
                changed = CareProposal.objects.filter(pk=pending.pk).update(
                    state="rejected",
                    decided_by_id=actor,
                    decided_at=pending.created_at,
                    revision=pending.revision + 1,
                )
            assert changed == 0
    finally:
        with owner_context(graph.organization_a):
            User.objects.filter(pk=world.physician).update(is_active=True)
    assert items_of(world, bundle.invocation_id)[0].state == "pending"
    assert len(items_of(world, bundle.invocation_id)) == len(bundle.items)


def test_runtime_cannot_call_the_internal_plan_status_helper(world: CareWorld) -> None:
    graph = world.graph
    unrelated, _enrollment = permission_actor(graph, "physician")
    actors = (
        (world.physician, True),
        (world.manager, True),
        (unrelated, True),
        (uuid4(), True),
        (world.physician, False),
    )
    try:
        for actor, active in actors:
            if actor == world.physician:
                with owner_context(graph.organization_a):
                    User.objects.filter(pk=actor).update(is_active=active)
            with permission_context(graph, actor):
                for plan in (world.plan.pk, uuid4()):
                    for revision in (world.plan.revision, world.plan.revision + 1):
                        with (
                            pytest.raises(DatabaseError) as denied,
                            transaction.atomic(),
                            connection.cursor() as cursor,
                        ):
                            cursor.execute(
                                "SELECT clinic_app.ai_care_plan_current(%s,%s)",
                                [plan, revision],
                            )
                        assert (
                            getattr(denied.value.__cause__, "sqlstate", None) == "42501"
                        )
                        assert (
                            "permission denied for function ai_care_plan_current"
                            in str(denied.value)
                        )
    finally:
        with owner_context(graph.organization_a):
            User.objects.filter(pk=world.physician).update(is_active=True)
    # The authorized resolver-owned trigger still uses the private predicate.
    assert request_and_collect(world).state == "ready"


def test_concurrent_bundle_approvals_add_each_action_once(world: CareWorld) -> None:
    bundle = request_and_collect(world)
    invocation = bundle.invocation_id
    assert invocation is not None
    before = len(actions_of(world))
    barrier = Barrier(2, timeout=20)

    def approver() -> tuple[str, ...]:
        connections.close_all()
        try:
            barrier.wait()
            try:
                return tuple(item.state for item in approve_bundle(world, invocation))
            except CareConflictError:
                return ("conflict",)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approver) for _ in range(2)]
        outcomes = sorted(future.result(timeout=60) for future in futures)
    # The row locks serialize the two staff: one approves, the other finds nothing.
    assert outcomes == [("approved", "approved"), ("conflict",)]
    assert len(actions_of(world)) == before + 2
    states = [item.state for item in items_of(world, invocation)]
    assert states == ["approved", "approved", "pending"]
