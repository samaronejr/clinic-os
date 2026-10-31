"""Todo 52 B1: a care action's task is gated and mirrored on every task path.

Staff work care actions in the Tasks and Inbox workspace, so these cases drive
the generic surface (``task_commands``, bulk reassignment) and raw SQL as
``clinic_app``, never only the care services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.workflows import task_commands
from apps.workflows.care import plans
from apps.workflows.care import services as care
from apps.workflows.care.models import CareAction
from apps.workflows.models import Task
from apps.workflows.reassignment import apply_reassignment, preview_reassignment
from apps.workflows.validation import TaskOwner, WorkflowConflictError
from django.db import DatabaseError, transaction
from django.db.models import F
from django.test import RequestFactory

from ehr.longitudinal_support import finalized, second_draft
from identity.permission_support import owner_context
from workflows.test_careplans import (
    acting,
    amended,
    manager,
    plan_of,
    return_in,
    rows,
    world,
)

if TYPE_CHECKING:
    from apps.ehr.models import ClinicalDocumentVersion

    from ehr.longitudinal_support import ChartWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def care_task(
    graph: RbacGraph, chart: ChartWorld | None = None
) -> tuple[CareAction, ClinicalDocumentVersion]:
    """One return-window action whose follow-up task is still open.

    Each chart seeds the physician's one appointment slot, so a second action
    in the same test comes from a new finalized note on that chart.
    """
    if chart is None:
        _chart, version = world(graph)
    else:
        user = chart.version.author_id
        version = finalized(graph, second_draft(graph, chart, user))
    plan = plan_of(graph, version, return_in(6))
    return rows(graph, CareAction, plan_id=plan.pk)[0], version


def current_plan(graph: RbacGraph, action: CareAction) -> UUID:
    return action_of(graph, action).plan_id


def task_id(action: CareAction) -> UUID:
    assert action.task_id is not None
    return action.task_id


def task_of(graph: RbacGraph, action: CareAction) -> Task:
    return rows(graph, Task, pk=task_id(action))[0]


def action_of(graph: RbacGraph, action: CareAction) -> CareAction:
    return rows(graph, CareAction, pk=action.pk)[0]


def command(graph: RbacGraph, actor: UUID, task: Task, **fields: str) -> None:
    """Post one generic task command exactly as the Tasks workspace does."""
    request = RequestFactory().post(
        "/",
        data={
            "task_id": str(task.pk),
            "expected_revision": str(task.revision),
            **fields,
        },
    )
    with acting(graph, actor):
        task_commands._action(request, graph.clinic_a, actor)


def completion(task: Task) -> dict[str, str]:
    return {
        "action": "complete",
        "evidence_kind": "reference",
        "record_kind": task.subject_ref["kind"],
        "record_id": task.subject_ref["id"],
        "outcome": "resolved",
    }


def events(graph: RbacGraph, record: UUID) -> list[str]:
    with owner_context(graph.organization_a):
        return list(
            AuditEvent.objects.filter(affected_record_id=str(record))
            .order_by("seq")
            .values_list("event_type", flat=True)
        )


def flagged(
    graph: RbacGraph, chart: ChartWorld | None = None
) -> tuple[CareAction, UUID]:
    """An assigned care action flagged after its source note was amended."""
    action, version = care_task(graph, chart)
    staff = manager(graph)
    with acting(graph, staff):
        care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=action.revision,
        )
    amended(graph, version)
    with acting(graph, staff):
        assert plans.flag_stale_care_plan(
            clinic_id=graph.clinic_a, plan_id=current_plan(graph, action)
        )
    current = action_of(graph, action)
    assert (current.review_state, current.state) == ("required", "assigned")
    return current, staff


def test_inbox_work_on_a_care_task_reaches_done_on_its_action(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    action, _version = care_task(graph)
    staff = manager(graph)
    command(graph, staff, task_of(graph, action), action="assign", owner="me")
    assigned = action_of(graph, action)
    assert (assigned.state, assigned.owner_user_id) == ("assigned", staff)
    command(graph, staff, task_of(graph, action), action="start")
    assert action_of(graph, action).state == "assigned"
    task = task_of(graph, action)
    command(graph, staff, task, **completion(task))
    assert task_of(graph, action).state == "done"
    assert action_of(graph, action).state == "done"
    assert events(graph, action.pk) == [
        "workflows.care_action.created",
        "workflows.care_action.assigned",
        "workflows.care_action.completed",
    ]


def test_cancelling_a_care_task_in_the_workspace_cancels_its_action(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    action, _version = care_task(graph)
    command(graph, manager(graph), task_of(graph, action), action="cancel")
    assert task_of(graph, action).state == "cancelled"
    assert action_of(graph, action).state == "cancelled"
    assert events(graph, action.pk)[-1] == "workflows.care_action.cancelled"


def test_flagged_care_task_refuses_workspace_work_and_still_cancels(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    action, staff = flagged(graph)
    other = manager(graph)
    for fields in (
        {"action": "assign", "owner": f"user:{other}"},
        {"action": "start"},
    ):
        with pytest.raises(WorkflowConflictError):
            command(graph, staff, task_of(graph, action), **fields)
    before = (task_of(graph, action), action_of(graph, action))
    assert (before[0].state, before[0].owner_user_id) == ("assigned", staff)
    assert before[1].revision == action.revision
    command(graph, staff, task_of(graph, action), action="cancel")
    assert task_of(graph, action).state == "cancelled"
    assert action_of(graph, action).state == "cancelled"


def test_stale_source_refuses_workspace_completion_before_any_scan(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    action, version = care_task(graph)
    staff = manager(graph)
    command(graph, staff, task_of(graph, action), action="assign", owner="me")
    command(graph, staff, task_of(graph, action), action="start")
    amended(graph, version)
    # Nothing flagged the action yet: the source itself decides.
    assert action_of(graph, action).review_state == "none"
    task = task_of(graph, action)
    with pytest.raises(WorkflowConflictError):
        command(graph, staff, task, **completion(task))
    assert task_of(graph, action).state == "in_progress"
    assert action_of(graph, action).state == "assigned"


def test_bulk_reassignment_refuses_a_flagged_care_task_and_mirrors_others(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    chart, version = world(graph)
    free = rows(graph, CareAction, plan_id=plan_of(graph, version, return_in(6)).pk)[0]
    held, staff = flagged(graph, chart)
    other = manager(graph)
    with acting(graph, staff), pytest.raises(WorkflowConflictError):
        preview_reassignment(
            clinic_id=graph.clinic_a,
            task_ids=[task_id(held), task_id(free)],
            owner=TaskOwner(user_id=other),
        )
    with acting(graph, staff):
        token = preview_reassignment(
            clinic_id=graph.clinic_a,
            task_ids=[task_id(free)],
            owner=TaskOwner(user_id=other),
        ).token
        apply_reassignment(clinic_id=graph.clinic_a, preview_token=token)
    moved = action_of(graph, free)
    assert (moved.state, moved.owner_user_id) == ("assigned", other)
    assert action_of(graph, held).owner_user_id == staff


def test_care_command_replays_after_the_task_moved_on(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    action, _version = care_task(graph)
    staff = manager(graph)
    with acting(graph, staff):
        first = care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=action.revision,
        )
        again = care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=action.revision,
        )
        assert (again.revision, again.last_command_key) == (
            first.revision,
            first.last_command_key,
        )
        with pytest.raises(WorkflowConflictError):
            care.assign_care_action(
                clinic_id=graph.clinic_a,
                action_id=action.pk,
                owner=TaskOwner(role="receptionist"),
                expected_revision=action.revision,
            )
    assert task_of(graph, action).revision == 2


def _bump(**fields: object) -> dict[str, object]:
    return {
        "revision": F("revision") + 1,
        "last_command_key": uuid4(),
        "last_command_digest": "a" * 64,
        **fields,
    }


def test_database_refuses_a_task_write_its_action_does_not_follow(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    action, _version = care_task(graph)
    staff = manager(graph)
    # Only the task moves: the deferred mirror check refuses the commit.
    with pytest.raises(DatabaseError) as caught, acting(graph, staff):
        Task.objects.filter(pk=task_id(action)).update(
            **_bump(owner_user_id=staff, state="assigned")
        )
    assert getattr(caught.value.__cause__, "sqlstate", None) == "23514"
    assert "care task left its action behind" in str(caught.value)
    # Only the action moves: refused at once.
    with (
        acting(graph, staff),
        pytest.raises(DatabaseError) as refused,
        transaction.atomic(),
    ):
        CareAction.objects.filter(pk=action.pk).update(
            **_bump(owner_user_id=staff, state="assigned")
        )
    assert "care action must follow its task" in str(refused.value)
    assert task_of(graph, action).state == "open"
    assert action_of(graph, action).state == "open"


def test_database_gate_refuses_raw_work_on_a_flagged_care_task(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    action, staff = flagged(graph)
    with (
        acting(graph, staff),
        pytest.raises(DatabaseError) as caught,
        transaction.atomic(),
    ):
        Task.objects.filter(pk=task_id(action)).update(**_bump(state="in_progress"))
    assert getattr(caught.value.__cause__, "sqlstate", None) == "23514"
    assert "care task awaits clinician review" in str(caught.value)
    assert task_of(graph, action).state == "assigned"
