"""Todo 52: care plans, referrals, recalls and PROs on real PostgreSQL roles."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.events import PHASE1_AUDIT_EVENTS
from apps.audit.models import AuditEvent
from apps.ehr.finalization import amend_document, finalize_version
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import record_clinical_note
from apps.identity.current_context import CurrentActorError
from apps.prescription.models import DocumentKind
from apps.tenancy.db import tenant_context
from apps.workflows.care import services as care
from apps.workflows.care import tasks as care_tasks
from apps.workflows.care.models import (
    CareAction,
    CarePlan,
    PROAssignment,
    PROOccurrence,
    Recall,
    Referral,
)
from apps.workflows.care.validation import (
    ADMIN_KINDS,
    CLINICAL_KINDS,
    CareActionSpec,
    PROSpec,
    ReferralSpec,
    return_window,
)
from apps.workflows.care.vocab import TASKLESS_KINDS
from apps.workflows.models import Task
from apps.workflows.validation import (
    TaskOwner,
    WorkflowConflictError,
    WorkflowInputError,
)
from django.db import DatabaseError, connection, transaction
from django.db.models import Model
from django.utils import timezone

from auth.stepup_test_support import verified_request
from ehr.longitudinal_support import ChartWorld, chart_world, finalized
from ehr.test_autosave import SECTIONS, as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from workflows.clock_support import database_clock
from workflows.permission_support import permission_actor

if TYPE_CHECKING:
    from collections.abc import Iterator

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


@contextmanager
def acting(graph: RbacGraph, user: UUID) -> Iterator[None]:
    with runtime_role(), tenant_context(user, graph.organization_a):
        yield


def world(graph: RbacGraph) -> tuple[ChartWorld, ClinicalDocumentVersion]:
    chart = chart_world(graph)
    return chart, finalized(graph, chart.version)


def return_in(weeks: int) -> CareActionSpec:
    start, end = return_window(now=timezone.now(), weeks=weeks)
    return CareActionSpec(kind="return_window", window_start=start, window_end=end)


def instruction() -> CareActionSpec:
    now = timezone.now()
    return CareActionSpec(
        kind="patient_instruction",
        window_start=now,
        window_end=now + timedelta(days=3),
        instruction="Manter hidratacao e repouso relativo.",
    )


def plan_of(
    graph: RbacGraph,
    version: ClinicalDocumentVersion,
    *specs: CareActionSpec,
    key: UUID | None = None,
) -> CarePlan:
    with acting(graph, version.author_id):
        return care.create_care_plan_from_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            actions=specs,
            idempotency_key=key or uuid4(),
        )


def amended(graph: RbacGraph, version: ClinicalDocumentVersion) -> UUID:
    request = verified_request(version.author_id)
    with as_actor(graph, version.author_id):
        draft = amend_document(
            clinic_id=graph.clinic_a, version_id=version.pk, reason="Correcao sintetica"
        )
        saved = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=1,
            content={**SECTIONS, "plan": "Retorno em oito semanas."},
        )
        return finalize_version(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=saved.revision,
            request=request,
        ).pk


def rows[M: Model](graph: RbacGraph, model: type[M], **filters: object) -> list[M]:
    with owner_context(graph.organization_a):
        return list(model._default_manager.filter(**filters))


def manager(graph: RbacGraph) -> UUID:
    return permission_actor(graph, "clinic_manager")[0]


def scan(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Run the care beat once and return the jobs it published."""
    published: list[dict[str, str]] = []

    def publish(*, kwargs: dict[str, str]) -> None:
        published.append(kwargs)

    monkeypatch.setattr(care_tasks.run_care_job, "apply_async", publish)
    with runtime_role():
        assert care_tasks.scan_care() == len(published)
    return published


def run_jobs(published: list[dict[str, str]]) -> list[bool]:
    """Run each published job as the worker does, outside any request."""
    with runtime_role():
        return [care_tasks.run_care(**job) for job in published]


def test_plan_from_final_version_yields_a_return_window_task(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6), instruction())
    assert plan.state == "active"
    assert plan.source_version_id == version.pk
    actions = {a.kind: a for a in rows(graph, CareAction, plan_id=plan.pk)}
    assert set(actions) == {"return_window", "patient_instruction"}
    window = actions["return_window"]
    assert window.state == "open"
    assert window.origin_version_id == version.pk
    assert window.review_state == "none"
    task = rows(graph, Task, pk=window.task_id)[0]
    assert (task.kind, task.state, task.due_at) == (
        "follow_up",
        "open",
        window.window_end,
    )
    assert task.subject_ref == {"kind": "enrollment", "id": str(chart.enrollment)}
    note = actions["patient_instruction"]
    assert note.task_id is None
    assert note.approved_by_id == version.author_id
    assert note.instruction == "Manter hidratacao e repouso relativo."


def test_creation_is_idempotent_and_changed_terms_conflict(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    key = uuid4()
    # A retry resends the identical terms; return_in() reads the wall clock.
    terms = return_in(6)
    first = plan_of(graph, version, terms, key=key)
    again = plan_of(graph, version, terms, key=key)
    assert again.pk == first.pk
    assert len(rows(graph, CareAction, plan_id=first.pk)) == 1
    with pytest.raises(WorkflowConflictError):
        plan_of(graph, version, return_in(8), key=key)


def test_draft_unknown_and_foreign_versions_are_refused_alike(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    chart = chart_world(graph)
    denials = []
    for clinic, version in (
        (graph.clinic_a, chart.version.pk),
        (graph.clinic_a, uuid4()),
        (graph.clinic_b, chart.version.pk),
    ):
        with (
            acting(graph, chart.version.author_id),
            pytest.raises((CurrentActorError, WorkflowConflictError)) as error,
        ):
            care.create_care_plan_from_version(
                clinic_id=clinic,
                version_id=version,
                actions=(return_in(6),),
                idempotency_key=uuid4(),
            )
        denials.append(error.value)
    # A draft is an unfinished source (conflict); unknown and wrong-clinic share one.
    assert isinstance(denials[0], WorkflowConflictError)
    assert str(denials[1]) == str(denials[2]) == "current actor unauthorized"
    assert not rows(graph, CarePlan)


def test_staff_cannot_author_a_plan_and_clinical_kinds_need_the_clinician(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    staff = manager(graph)
    with acting(graph, staff), pytest.raises(CurrentActorError):
        care.create_care_plan_from_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            actions=(return_in(6),),
            idempotency_key=uuid4(),
        )
    plan = plan_of(graph, version, return_in(6))
    # Administrative kinds can be added by staff; patient-facing instructions cannot.
    with acting(graph, staff):
        added = care.add_care_action(
            clinic_id=graph.clinic_a,
            plan_id=plan.pk,
            spec=CareActionSpec(
                kind="staff_coordination",
                window_start=timezone.now(),
                window_end=timezone.now() + timedelta(days=2),
            ),
            idempotency_key=uuid4(),
        )
        assert added.approved_by_id is None
        with pytest.raises(CurrentActorError):
            care.add_care_action(
                clinic_id=graph.clinic_a,
                plan_id=plan.pk,
                spec=instruction(),
                idempotency_key=uuid4(),
            )
    assert not rows(graph, CareAction, plan_id=plan.pk, kind="patient_instruction")


# Which authority adds each action kind; a new kind needs a decision here.
AUTHORITY = {
    "return_window": "staff",
    "staff_coordination": "staff",
    "order": "clinician",
    "referral": "clinician",
    "questionnaire": "clinician",
    "monitoring_task": "clinician",
    "patient_instruction": "clinician",
}


def kind_spec(kind: str) -> CareActionSpec:
    now = timezone.now()
    return CareActionSpec(
        kind=kind,
        window_start=now,
        window_end=now + timedelta(days=3),
        instruction="Repouso relativo." if kind == "patient_instruction" else "",
        referral=(
            ReferralSpec(target_kind="specialty", target_label="Cardiologia")
            if kind == "referral"
            else None
        ),
    )


@pytest.mark.parametrize("kind", sorted(ADMIN_KINDS | CLINICAL_KINDS))
def test_each_action_kind_is_added_only_by_its_authority(
    rbac_graph: RbacGraph, kind: str
) -> None:
    # Replaces a vocabulary restatement: the service and the SQL guard must
    # both enforce the same authority for every kind.
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))

    def add(actor: UUID) -> CareAction:
        with acting(graph, actor):
            return care.add_care_action(
                clinic_id=graph.clinic_a,
                plan_id=plan.pk,
                spec=kind_spec(kind),
                idempotency_key=uuid4(),
            )

    if AUTHORITY[kind] == "clinician":
        with pytest.raises(CurrentActorError):
            add(manager(graph))
        added = add(version.author_id)
        assert added.approved_by_id == version.author_id
    else:
        added = add(manager(graph))
        assert added.approved_by_id is None
    assert (added.task_id is None) == (kind in TASKLESS_KINDS)


def test_database_refuses_an_unapproved_patient_instruction(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))
    staff = manager(graph)
    now = timezone.now()
    with (
        acting(graph, staff),
        pytest.raises(DatabaseError) as caught,
        transaction.atomic(),
    ):
        CareAction.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            plan_id=plan.pk,
            kind="patient_instruction",
            window_start=now,
            window_end=now + timedelta(days=1),
            origin_version_id=version.pk,
            created_by_id=staff,
            approved_by_id=staff,
            instruction="sem aprovacao",
            idempotency_key=uuid4(),
            fingerprint="0" * 64,
        )
    # The clinician-approval refusal itself, not any database error.
    assert getattr(caught.value.__cause__, "sqlstate", None) == "42501"
    assert "care action requires clinician approval" in str(caught.value)


def test_staff_execute_the_administrative_action_through_the_single_task(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))
    action = rows(graph, CareAction, plan_id=plan.pk)[0]
    staff = manager(graph)
    with acting(graph, staff):
        assigned = care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=action.revision,
        )
        assert (assigned.state, assigned.owner_user_id) == ("assigned", staff)
        with pytest.raises(WorkflowConflictError):
            care.complete_care_action(
                clinic_id=graph.clinic_a,
                action_id=action.pk,
                outcome="resolved",
                expected_revision=action.revision,
            )
        done = care.complete_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            outcome="resolved",
            expected_revision=assigned.revision,
        )
    assert done.state == "done"
    task = rows(graph, Task, pk=action.task_id)[0]
    assert task.state == "done"
    assert task.completion_evidence["outcome"] == "resolved"


def test_amended_source_flags_pending_actions_for_clinician_review(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6), instruction())
    assert rows(graph, ClinicalDocumentVersion, pk=version.pk)[0].state == "finalized"
    amended(graph, version)
    staff = manager(graph)
    jobs = scan(monkeypatch)
    assert jobs == [{"kind": "stale", "record_id": str(plan.pk)}]
    assert run_jobs(jobs) == [True]
    # Flagged actions wait for the clinician: nothing is listed again.
    assert scan(monkeypatch) == []
    refreshed = rows(graph, CarePlan, pk=plan.pk)[0]
    assert refreshed.state == "superseded"
    flagged = rows(graph, CareAction, plan_id=plan.pk)
    assert {a.review_state for a in flagged} == {"required"}
    window = next(a for a in flagged if a.kind == "return_window")
    with acting(graph, staff):
        with pytest.raises(WorkflowConflictError):
            care.assign_care_action(
                clinic_id=graph.clinic_a,
                action_id=window.pk,
                owner=TaskOwner(user_id=staff),
                expected_revision=window.revision,
            )
        with pytest.raises(WorkflowConflictError):
            care.complete_care_action(
                clinic_id=graph.clinic_a,
                action_id=window.pk,
                outcome="resolved",
                expected_revision=window.revision,
            )
    # Staff cannot clear the flag; the clinician keeps one and cancels the other.
    with acting(graph, staff), pytest.raises(CurrentActorError):
        care.review_care_action(
            clinic_id=graph.clinic_a,
            action_id=window.pk,
            decision="confirm",
            expected_revision=window.revision,
        )
    with acting(graph, version.author_id):
        kept = care.review_care_action(
            clinic_id=graph.clinic_a,
            action_id=window.pk,
            decision="confirm",
            expected_revision=window.revision,
        )
        note = next(a for a in flagged if a.kind == "patient_instruction")
        dropped = care.review_care_action(
            clinic_id=graph.clinic_a,
            action_id=note.pk,
            decision="cancel",
            expected_revision=note.revision,
        )
    assert (kept.review_state, kept.state) == ("confirmed", "open")
    assert dropped.state == "cancelled"


def test_overdue_marking_follows_the_injected_clock(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))
    action = rows(graph, CareAction, plan_id=plan.pk)[0]
    base = timezone.now()
    clock = {"now": base}
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        assert scan(monkeypatch) == []
        clock["now"] = base + timedelta(weeks=8)
        advance()
        jobs = scan(monkeypatch)
        assert jobs == [{"kind": "overdue", "record_id": str(action.pk)}]
        assert run_jobs(jobs) == [True]
        assert scan(monkeypatch) == []
    assert rows(graph, CareAction, plan_id=plan.pk)[0].state == "overdue"


def test_referral_links_only_a_same_encounter_referral_document(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    chart, version = world(graph)
    start, end = return_window(now=timezone.now(), weeks=2)
    spec = CareActionSpec(
        kind="referral",
        window_start=start,
        window_end=end,
        referral=ReferralSpec(target_kind="specialty", target_label="Cardiologia"),
    )
    plan = plan_of(graph, version, spec)
    referral = rows(graph, Referral, plan_id=plan.pk)[0]
    assert (referral.status, referral.document_id) == ("requested", None)
    from apps.prescription.services import create_document_draft  # noqa: PLC0415

    with as_actor(graph, version.author_id):
        draft = create_document_draft(
            clinic_id=graph.clinic_a,
            encounter_id=plan.encounter_id,
            kind=DocumentKind.REFERRAL,
        )
        instruction_draft = create_document_draft(
            clinic_id=graph.clinic_a,
            encounter_id=plan.encounter_id,
            kind=DocumentKind.INSTRUCTION,
        )
    with acting(graph, version.author_id):
        with pytest.raises(WorkflowConflictError):
            care.link_referral_document(
                clinic_id=graph.clinic_a,
                referral_id=referral.pk,
                draft_id=instruction_draft.pk,
                expected_revision=referral.revision,
            )
        linked = care.link_referral_document(
            clinic_id=graph.clinic_a,
            referral_id=referral.pk,
            draft_id=draft.pk,
            expected_revision=referral.revision,
        )
        assert linked.document_id == draft.pk
        scheduled = care.advance_referral(
            clinic_id=graph.clinic_a,
            referral_id=referral.pk,
            status="scheduled",
            expected_revision=linked.revision,
        )
        assert scheduled.status == "scheduled"
        with pytest.raises(WorkflowConflictError):
            care.advance_referral(
                clinic_id=graph.clinic_a,
                referral_id=referral.pk,
                status="requested",
                expected_revision=scheduled.revision,
            )
    assert chart.enrollment


def test_recall_due_date_uses_the_injected_clock_and_materializes_once(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    chart = chart_world(graph)
    anchor = timezone.now()
    clock = {"now": anchor}
    staff = manager(graph)
    key = uuid4()

    def schedule(days: int = 90, request_key: UUID = key) -> Recall:
        return care.schedule_recall(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            reason="routine_return",
            interval_days=days,
            owner_role="receptionist",
            idempotency_key=request_key,
        )

    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        with acting(graph, staff):
            recall = schedule()
            assert recall.due_at == anchor + timedelta(days=90)
            assert recall.state == "scheduled"
            assert schedule().pk == recall.pk
            for days in (0, 731):
                with pytest.raises(WorkflowInputError):
                    schedule(days, uuid4())
        assert scan(monkeypatch) == []
        clock["now"] = anchor + timedelta(days=91)
        advance()
        jobs = scan(monkeypatch)
        assert jobs == [{"kind": "recall", "record_id": str(recall.pk)}]
        assert run_jobs(jobs) == [True]
        assert scan(monkeypatch) == []
    done = rows(graph, Recall, pk=recall.pk)[0]
    assert done.state == "materialized"
    task = rows(graph, Task, pk=done.task_id)[0]
    assert (task.owner_role, task.state, task.kind) == (
        "receptionist",
        "assigned",
        "follow_up",
    )


def pro_terms(**changes: object) -> PROSpec:
    terms: dict[str, object] = {
        "instrument_key": "phq9",
        "first_due_at": timezone.now() + timedelta(days=7),
        "interval_days": 7,
        "occurrences": 3,
        "response_role": "physician",
        "staffing_policy_ref": "eg3-pending-staffing-policy",
    }
    terms.update(changes)
    return PROSpec(**terms)  # type: ignore[arg-type]


def test_pro_assignment_schedules_occurrences_for_an_available_instrument(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    chart, version = world(graph)
    with acting(graph, version.author_id):
        assignment = care.assign_pro(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            spec=pro_terms(),
            idempotency_key=uuid4(),
        )
        for key in ("growth_percentile", "bmi"):
            with pytest.raises(WorkflowInputError):
                care.assign_pro(
                    clinic_id=graph.clinic_a,
                    enrollment_id=chart.enrollment,
                    spec=pro_terms(instrument_key=key),
                    idempotency_key=uuid4(),
                )
    stored = rows(graph, PROOccurrence, assignment_id=assignment.pk)
    assert [o.sequence for o in sorted(stored, key=lambda o: o.sequence)] == [1, 2, 3]
    assert {o.state for o in stored} == {"pending"}
    assert assignment.response_role == "physician"
    assert assignment.staffing_policy_ref == "eg3-pending-staffing-policy"


def test_staff_cannot_assign_a_pro(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    chart, _version = world(graph)
    with acting(graph, manager(graph)), pytest.raises(CurrentActorError):
        care.assign_pro(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            spec=pro_terms(),
            idempotency_key=uuid4(),
        )
    assert not rows(graph, PROAssignment)


def test_missing_response_is_sem_resposta_and_creates_no_inference(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    chart, version = world(graph)
    start = timezone.now()
    clock = {"now": start}
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        with acting(graph, version.author_id):
            assignment = care.assign_pro(
                clinic_id=graph.clinic_a,
                enrollment_id=chart.enrollment,
                spec=pro_terms(first_due_at=start + timedelta(days=1), occurrences=2),
                idempotency_key=uuid4(),
            )
        assert scan(monkeypatch) == []
        clock["now"] = start + timedelta(days=30)
        advance()
        jobs = scan(monkeypatch)
        assert {job["kind"] for job in jobs} == {"pro_missed"}
        assert run_jobs(jobs) == [True, True]
        assert scan(monkeypatch) == []
    stored = rows(graph, PROOccurrence, assignment_id=assignment.pk)
    assert {o.state for o in stored} == {"no_response"}
    assert all(o.concerning is False and o.response_task_id is None for o in stored)
    assert all(o.answers is None for o in stored)
    assert rows(graph, PROAssignment, pk=assignment.pk)[0].state == "completed"


def test_responses_reach_a_human_queue_and_item_nine_is_urgent(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    chart, version = world(graph)
    with acting(graph, version.author_id):
        assignment = care.assign_pro(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            spec=pro_terms(occurrences=2),
            idempotency_key=uuid4(),
        )
    first, second = sorted(
        rows(graph, PROOccurrence, assignment_id=assignment.pk),
        key=lambda o: o.sequence,
    )
    calm = {f"item_{n}": "0" for n in range(1, 10)}
    urgent = {**calm, "item_9": "1"}
    # The patient's clinician records; staff routing authority never does (B6).
    with acting(graph, version.author_id):
        quiet = care.record_pro_response(
            clinic_id=graph.clinic_a,
            occurrence_id=first.pk,
            answers=calm,
            expected_revision=first.revision,
        )
        loud = care.record_pro_response(
            clinic_id=graph.clinic_a,
            occurrence_id=second.pk,
            answers=urgent,
            expected_revision=second.revision,
        )
        with pytest.raises(WorkflowInputError):
            care.record_pro_response(
                clinic_id=graph.clinic_a,
                occurrence_id=second.pk,
                answers={"item_1": "0"},
                expected_revision=loud.revision,
            )
    assert (quiet.state, quiet.concerning) == ("responded", False)
    assert (loud.state, loud.concerning) == ("responded", True)
    tasks = {o.pk: rows(graph, Task, pk=o.response_task_id)[0] for o in (quiet, loud)}
    assert tasks[quiet.pk].priority == "normal"
    assert tasks[loud.pk].priority == "urgent"
    assert {t.owner_role for t in tasks.values()} == {"physician"}
    assert {(t.state, t.created_by_id) for t in tasks.values()} == {("assigned", None)}
    assert rows(graph, PROAssignment, pk=assignment.pk)[0].state == "completed"


def sql_refused(sqlstate: str, statement: str, identifier: UUID) -> str:
    with (
        pytest.raises(DatabaseError) as caught,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(statement, [identifier])
    assert getattr(caught.value.__cause__, "sqlstate", None) == sqlstate
    return str(caught.value)


def test_database_refuses_care_history_deletes_and_rewrites(
    rbac_graph: RbacGraph,
) -> None:
    # Raw SQL: an ORM-side PROTECT check cannot stand in for the database.
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))
    action = rows(graph, CareAction, plan_id=plan.pk)[0]
    with acting(graph, version.author_id):
        # No DELETE grant and no grant on identity columns for the runtime role.
        for statement, identifier in (
            ("DELETE FROM clinic_app.workflows_careplan WHERE id=%s", plan.pk),
            ("DELETE FROM clinic_app.workflows_careaction WHERE id=%s", action.pk),
            (
                "UPDATE clinic_app.workflows_careplan "
                "SET source_version_id=gen_random_uuid() WHERE id=%s",
                plan.pk,
            ),
            (
                "UPDATE clinic_app.workflows_careaction "
                "SET window_end=statement_timestamp() WHERE id=%s",
                action.pk,
            ),
        ):
            sql_refused("42501", statement, identifier)
    # The owner holds DELETE on its tables; the care guard still refuses it.
    with owner_context(graph.organization_a):
        for statement, identifier in (
            ("DELETE FROM clinic_app.workflows_careaction WHERE id=%s", action.pk),
            ("DELETE FROM clinic_app.workflows_careplan WHERE id=%s", plan.pk),
        ):
            refusal = sql_refused("23514", statement, identifier)
            assert "care history is immutable" in refusal
    assert rows(graph, CarePlan, pk=plan.pk)
    assert rows(graph, CareAction, pk=action.pk)


CARE_EVENTS = frozenset(
    name
    for name in PHASE1_AUDIT_EVENTS
    if name.split(".")[0] == "workflows"
    and name.split(".")[1]
    in {"care_plan", "care_action", "referral", "recall", "pro", "pro_occurrence"}
)


def test_every_care_transition_audits_its_registered_event_and_metadata_only(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each care transition runs once: the ledger must hold every registered
    # care event (none unaudited) and only the fixed metadata keys.
    from apps.prescription.services import create_document_draft  # noqa: PLC0415

    graph = rbac_graph
    _chart, version = world(graph)
    clinician, staff = version.author_id, manager(graph)
    base = timezone.now()
    clock = {"now": base}
    short = CareActionSpec(
        kind="staff_coordination",
        window_start=base,
        window_end=base + timedelta(days=1),
    )
    refer = CareActionSpec(
        kind="referral",
        window_start=base,
        window_end=base + timedelta(days=14),
        referral=ReferralSpec(target_kind="specialty", target_label="Cardiologia"),
    )
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:

        def at(days: int) -> None:
            clock["now"] = base + timedelta(days=days)
            advance()

        plan = plan_of(graph, version, return_in(6), short, short, instruction(), refer)
        actions = rows(graph, CareAction, plan_id=plan.pk)
        window = next(a for a in actions if a.kind == "return_window")
        cancelled, late = (a for a in actions if a.kind == "staff_coordination")
        note = next(a for a in actions if a.kind == "patient_instruction")
        (referral,) = rows(graph, Referral, plan_id=plan.pk)
        with acting(graph, staff):
            assigned = care.assign_care_action(
                clinic_id=graph.clinic_a,
                action_id=window.pk,
                owner=TaskOwner(user_id=staff),
                expected_revision=window.revision,
            )
            care.complete_care_action(
                clinic_id=graph.clinic_a,
                action_id=window.pk,
                outcome="resolved",
                expected_revision=assigned.revision,
            )
            care.cancel_care_action(
                clinic_id=graph.clinic_a,
                action_id=cancelled.pk,
                expected_revision=cancelled.revision,
            )
        with acting(graph, clinician):
            draft = create_document_draft(
                clinic_id=graph.clinic_a,
                encounter_id=plan.encounter_id,
                kind=DocumentKind.REFERRAL,
            )
            linked = care.link_referral_document(
                clinic_id=graph.clinic_a,
                referral_id=referral.pk,
                draft_id=draft.pk,
                expected_revision=referral.revision,
            )
            care.advance_referral(
                clinic_id=graph.clinic_a,
                referral_id=referral.pk,
                status="scheduled",
                expected_revision=linked.revision,
            )
        at(2)
        assert all(run_jobs(scan(monkeypatch)))
        assert rows(graph, CareAction, pk=late.pk)[0].state == "overdue"
        amended(graph, version)
        assert all(run_jobs(scan(monkeypatch)))  # superseded; pending work flagged
        (flagged,) = rows(graph, CareAction, pk=note.pk)
        with acting(graph, clinician):
            care.review_care_action(
                clinic_id=graph.clinic_a,
                action_id=note.pk,
                decision="confirm",
                expected_revision=flagged.revision,
            )
            series = care.assign_pro(
                clinic_id=graph.clinic_a,
                enrollment_id=plan.enrollment_id,
                spec=pro_terms(
                    first_due_at=clock["now"] + timedelta(days=1), occurrences=2
                ),
                idempotency_key=uuid4(),
            )
        with acting(graph, staff):
            care.schedule_recall(
                clinic_id=graph.clinic_a,
                enrollment_id=plan.enrollment_id,
                reason="routine_return",
                interval_days=1,
                owner_role="receptionist",
                idempotency_key=uuid4(),
            )
        first = min(
            rows(graph, PROOccurrence, assignment_id=series.pk),
            key=lambda slot: slot.sequence,
        )
        with acting(graph, clinician):
            care.record_pro_response(
                clinic_id=graph.clinic_a,
                occurrence_id=first.pk,
                answers={f"item_{n}": "0" for n in range(1, 10)},
                expected_revision=first.revision,
            )
        # The recall falls due and the second slot passes its grace period.
        at(16)
        assert all(run_jobs(scan(monkeypatch)))
    with owner_context(graph.organization_a):
        events = list(AuditEvent.objects.filter(event_type__in=CARE_EVENTS))
    assert {event.event_type for event in events} == CARE_EVENTS
    for event in events:
        assert set(event.payload) <= SAFE_KEYS
        assert "Manter hidratacao" not in str(event.payload)
        assert "Cardiologia" not in str(event.payload)


def test_amended_source_refuses_assign_and_complete_before_any_scan(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6), return_in(8))
    first, second = sorted(
        rows(graph, CareAction, plan_id=plan.pk), key=lambda a: a.window_end
    )
    staff = manager(graph)
    with acting(graph, staff):
        assigned = care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=first.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=first.revision,
        )
    # Amend the source and act at once: no scanner has run, nothing is flagged.
    amended(graph, version)
    assert rows(graph, CareAction, pk=first.pk)[0].review_state == "none"
    with acting(graph, staff), pytest.raises(WorkflowConflictError):
        care.complete_care_action(
            clinic_id=graph.clinic_a,
            action_id=first.pk,
            outcome="resolved",
            expected_revision=assigned.revision,
        )
    with acting(graph, staff), pytest.raises(WorkflowConflictError):
        care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=second.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=second.revision,
        )
    # The refusals rolled back: the task and both actions are exactly as before.
    assert rows(graph, Task, pk=first.task_id)[0].state == "assigned"
    assert rows(graph, CareAction, pk=first.pk)[0].state == "assigned"
    after = rows(graph, CareAction, pk=second.pk)[0]
    assert (after.state, after.owner_user_id) == ("open", None)


def test_database_refuses_stale_source_work_even_without_the_service(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, instruction())
    note = rows(graph, CareAction, plan_id=plan.pk)[0]
    amended(graph, version)
    with (
        acting(graph, version.author_id),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        CareAction.objects.filter(pk=note.pk).update(
            state="done",
            revision=note.revision + 1,
            last_command_key=uuid4(),
            last_command_digest="0" * 64,
        )
    assert rows(graph, CareAction, pk=note.pk)[0].state == "open"


def test_clinician_confirmation_reopens_a_flagged_action_for_staff(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))
    action = rows(graph, CareAction, plan_id=plan.pk)[0]
    amended(graph, version)
    staff = manager(graph)
    assert run_jobs(scan(monkeypatch)) == [True]
    flagged = rows(graph, CareAction, pk=action.pk)[0]
    with acting(graph, version.author_id):
        kept = care.review_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            decision="confirm",
            expected_revision=flagged.revision,
        )
    with acting(graph, staff):
        assigned = care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=kept.revision,
        )
        done = care.complete_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            outcome="resolved",
            expected_revision=assigned.revision,
        )
    assert (assigned.review_state, done.state) == ("confirmed", "done")


def test_pro_series_cannot_start_behind_the_clock_but_replays_after_it_passes(
    rbac_graph: RbacGraph,
) -> None:
    # Review N10: a slot due before its series exists is closed 'no_response' at once.
    graph = rbac_graph
    chart, version = world(graph)
    start = timezone.now()
    clock = {"now": start}
    ahead = pro_terms(first_due_at=start + timedelta(days=1))
    key = uuid4()

    def assign(request_key: UUID) -> PROAssignment:
        with acting(graph, version.author_id):
            return care.assign_pro(
                clinic_id=graph.clinic_a,
                enrollment_id=chart.enrollment,
                spec=ahead,
                idempotency_key=request_key,
            )

    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        assignment = assign(key)
        clock["now"] = start + timedelta(days=2)
        advance()
        # Ahead of the wall clock but behind the database clock: refused ...
        with pytest.raises(WorkflowInputError):
            assign(uuid4())
        # ... while an identical retry still returns the existing series.
        assert assign(key).pk == assignment.pk
