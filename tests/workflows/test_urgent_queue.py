"""Todo 55: urgent-concern queue and result release coordination, on real roles.

Escalation runs on a monkeypatched queue clock; the guardian case revokes the
delegate grant between the release and the outbox send.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from apps.audit.models import AuditEvent
from apps.comms.inbox_services import PROVIDER_BY_CHANNEL
from apps.comms.models import IntegrationOperation, Message
from apps.core import integration
from apps.identity.current_context import CurrentActorError
from apps.identity.delegates import revoke
from apps.intake.patient_access import patient_session_context
from apps.tenancy.db import tenant_context
from apps.workflows import urgent
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.models import Task
from apps.workflows.release import (
    publish_release_notice,
    release_deliveries,
    release_result_to_patient,
)
from apps.workflows.release_models import ReleaseDelivery
from apps.workflows.urgent import (
    NO_COVERAGE,
    Coverage,
    acknowledge_urgent_concern,
    due_at,
    patient_coverage_text,
    resolve_urgent_concern,
    urgent_concerns,
)
from apps.workflows.urgent_models import UrgentConcern, UrgentCoveragePolicy
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError
from django.db import DatabaseError, transaction
from django.utils.translation import gettext

from comms.support import manager_of
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role
from workflows.coordination_support import (
    NOTICE,
    ROUTINE,
    URGENT,
    patient_reply,
    release_world,
    run_operation,
    urgent_world,
)
from workflows.permission_support import permission_actor

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

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
ZONE = ZoneInfo("America/Sao_Paulo")


def _concerns(graph: RbacGraph) -> list[UrgentConcern]:
    with owner_context(graph.organization_a):
        return list(UrgentConcern.objects.select_related("policy").order_by("pk"))


def _assert_follow_up(task: Task, enrollment: UUID) -> None:
    """One source-backed reception task; no staff identity was borrowed."""
    assert (task.kind, task.priority, task.state) == ("follow_up", "high", "assigned")
    assert (task.owner_role, task.owner_user_id, task.created_by_id) == (
        "receptionist",
        None,
        None,
    )
    assert task.subject_ref == {"kind": "enrollment", "id": str(enrollment)}


def _message(delivery: ReleaseDelivery) -> UUID:
    """The portal notice a queued delivery carries."""
    assert delivery.message_id is not None
    return delivery.message_id


def test_business_hours_deadline_skips_closed_days_and_hours() -> None:
    weekdays = Coverage(1, (0, 1, 2, 3, 4), time(8, 0), time(18, 0), 4, ("nurse",))
    # Friday 16:00 local: two hours on Friday, two more on Monday from 08:00.
    friday = datetime(2031, 3, 7, 16, 0, tzinfo=ZONE)
    assert due_at(friday, weekdays, ZONE) == datetime(
        2031, 3, 10, 10, 0, tzinfo=ZONE
    ).astimezone(UTC)
    # Saturday night starts the clock on Monday morning.
    saturday = datetime(2031, 3, 8, 22, 0, tzinfo=ZONE)
    assert due_at(saturday, weekdays, ZONE) == datetime(
        2031, 3, 10, 12, 0, tzinfo=ZONE
    ).astimezone(UTC)
    assert due_at(friday, None, ZONE) is None


def test_narrow_weekly_coverage_keeps_the_full_response_deadline() -> None:
    coverage = Coverage(1, (0,), time(8), time(9), 72, ("nurse",))
    monday = datetime(2031, 3, 3, 8, tzinfo=ZONE)
    assert due_at(monday, coverage, ZONE) == (
        monday + timedelta(weeks=71, hours=1)
    ).astimezone(UTC)
    shortest = Coverage(1, (0,), time(8), time(8, 1), 72, ("nurse",))
    assert due_at(monday, shortest, ZONE) == (
        monday + timedelta(weeks=4319, minutes=1)
    ).astimezone(UTC)


@pytest.mark.parametrize(
    ("opens_at", "closes_at"),
    [
        (time(8, 0, 1), time(18)),
        (time(8, 0, 0, 1), time(18)),
        (time(8), time(18, 0, 1)),
        (time(8), time(18, 0, 0, 1)),
        (time(8, tzinfo=UTC), time(18)),
        (time(8), time(18, tzinfo=UTC)),
    ],
)
def test_coverage_accepts_only_naive_whole_minutes(
    rbac_graph: RbacGraph, opens_at: time, closes_at: time
) -> None:
    manager = manager_of(rbac_graph)
    with (
        runtime_role(),
        tenant_context(manager, rbac_graph.organization_a),
        pytest.raises(WorkflowInputError),
    ):
        urgent.set_urgent_coverage(
            clinic_id=rbac_graph.clinic_a,
            expected_version=0,
            weekdays=(0,),
            opens_at=opens_at,
            closes_at=closes_at,
            response_hours=1,
            responsible_roles=("nurse",),
        )


@pytest.mark.parametrize(
    ("opens_at", "closes_at"),
    [
        (time(8, 0, 1), time(18)),
        (time(8, 0, 0, 1), time(18)),
        (time(8), time(18, 0, 1)),
        (time(8), time(18, 0, 0, 1)),
    ],
)
def test_database_rejects_undisclosed_coverage_precision(
    rbac_graph: RbacGraph, opens_at: time, closes_at: time
) -> None:
    manager = manager_of(rbac_graph)
    with (
        runtime_role(),
        tenant_context(manager, rbac_graph.organization_a),
        pytest.raises(DatabaseError, match="coverage refused"),
        transaction.atomic(),
    ):
        UrgentCoveragePolicy.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            version=1,
            weekdays=[0],
            opens_at=opens_at,
            closes_at=closes_at,
            response_hours=1,
            responsible_roles=["nurse"],
            created_by_id=manager,
        )


def test_new_overdue_concern_is_not_hidden_by_older_long_window_concerns(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = urgent_world(rbac_graph, roles=("nurse",), hours=72)
    graph = world.graph
    for _ in range(200):
        patient_reply(world, URGENT)
    manager = manager_of(graph)
    with runtime_role(), tenant_context(manager, graph.organization_a):
        urgent.set_urgent_coverage(
            clinic_id=graph.clinic_a,
            expected_version=1,
            weekdays=(0, 1, 2, 3, 4, 5, 6),
            opens_at=time(0),
            closes_at=time(23, 59),
            response_hours=1,
            responsible_roles=("nurse",),
        )
    message = patient_reply(world, URGENT)
    with owner_context(graph.organization_a):
        concern = UrgentConcern.objects.get(message_id=message)
    now = concern.reported_at + timedelta(hours=2)
    monkeypatch.setattr(urgent, "utc_now", lambda: now)
    with as_actor(graph, manager):
        [row] = urgent_concerns(clinic_id=graph.clinic_a)
    assert (row.concern_id, row.overdue, row.responsible) == (
        concern.pk,
        True,
        False,
    )


def test_urgent_reply_opens_one_concern_and_escalates_when_overdue(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = urgent_world(rbac_graph, roles=("nurse",), hours=1)
    graph = world.graph
    patient_reply(world, ROUTINE)
    assert _concerns(graph) == []
    key = uuid4()
    patient_reply(world, URGENT, key)
    patient_reply(world, URGENT, key)  # the same form key stores nothing twice
    [concern] = _concerns(graph)
    assert (concern.state, concern.enrollment_id) == ("open", world.enrollment)
    assert concern.policy is not None
    deadline = due_at(
        concern.reported_at,
        Coverage(
            concern.policy.version,
            tuple(concern.policy.weekdays),
            concern.policy.opens_at,
            concern.policy.closes_at,
            concern.policy.response_hours,
            tuple(concern.policy.responsible_roles),
        ),
        ZoneInfo("America/Sao_Paulo"),
    )
    assert deadline is not None
    manager, _enrollment = permission_actor(graph, "clinic_manager")
    # Before the deadline: the responsible nurse sees it, the manager does not.
    monkeypatch.setattr(urgent, "utc_now", lambda: deadline - timedelta(minutes=1))
    with as_actor(graph, world.nurse):
        [row] = urgent_concerns(clinic_id=graph.clinic_a)
        assert (row.overdue, row.responsible) == (False, True)
    with as_actor(graph, manager):
        assert urgent_concerns(clinic_id=graph.clinic_a) == ()
    # At the deadline it is overdue and escalates to the clinic's managers.
    monkeypatch.setattr(urgent, "utc_now", lambda: deadline)
    with as_actor(graph, manager):
        [row] = urgent_concerns(clinic_id=graph.clinic_a)
        assert (row.overdue, row.responsible) == (True, False)
    with as_actor(graph, world.nurse):
        [row] = urgent_concerns(clinic_id=graph.clinic_a)
        assert row.overdue
    with owner_context(graph.organization_a):
        payloads = list(
            AuditEvent.objects.filter(
                organization_id=graph.organization_a,
                event_type="workflows.urgent_concern.reported",
            ).values_list("payload", flat=True)
        )
    assert len(payloads) == 1
    assert all(set(payload) <= SAFE_KEYS for payload in payloads)
    assert URGENT not in json.dumps(payloads)


def test_only_responsible_roles_acknowledge_and_resolve(rbac_graph: RbacGraph) -> None:
    world = urgent_world(rbac_graph, roles=("nurse",))
    graph = world.graph
    patient_reply(world, URGENT)
    [concern] = _concerns(graph)
    receptionist, _enrollment = permission_actor(graph, "receptionist")
    with as_actor(graph, receptionist), pytest.raises(WorkflowAccessDeniedError):
        acknowledge_urgent_concern(
            clinic_id=graph.clinic_a, concern_id=concern.pk, expected_revision=1
        )
    for clinic, selector in ((graph.clinic_a, uuid4()), (graph.clinic_b, concern.pk)):
        with as_actor(graph, world.nurse), pytest.raises(WorkflowAccessDeniedError):
            acknowledge_urgent_concern(
                clinic_id=clinic, concern_id=selector, expected_revision=1
            )
    with as_actor(graph, world.nurse):
        acked = acknowledge_urgent_concern(
            clinic_id=graph.clinic_a, concern_id=concern.pk, expected_revision=1
        )
        assert acked.state == "acknowledged"
        with pytest.raises(WorkflowConflictError):
            resolve_urgent_concern(
                clinic_id=graph.clinic_a, concern_id=concern.pk, expected_revision=1
            )
        resolved = resolve_urgent_concern(
            clinic_id=graph.clinic_a, concern_id=concern.pk, expected_revision=2
        )
    assert resolved.state == "resolved"
    [stored] = _concerns(graph)
    assert (stored.acknowledged_by_id, stored.resolved_by_id) == (
        world.nurse,
        world.nurse,
    )


def test_portal_states_only_the_configured_coverage(rbac_graph: RbacGraph) -> None:
    bare = urgent_world(rbac_graph, coverage=False)
    with runtime_role(), patient_session_context(bare.session):
        assert patient_coverage_text() == (gettext(NO_COVERAGE),)
    # The graph's clinic_admin administers clinic B; clinic A needs its own admin.
    manager = manager_of(rbac_graph)
    with (
        runtime_role(),
        tenant_context(manager, rbac_graph.organization_a),
    ):
        urgent.set_urgent_coverage(
            clinic_id=rbac_graph.clinic_a,
            expected_version=0,
            weekdays=(0, 1, 2, 3, 4),
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            response_hours=4,
            responsible_roles=("nurse", "physician"),
        )
        with pytest.raises(WorkflowConflictError):
            urgent.set_urgent_coverage(
                clinic_id=rbac_graph.clinic_a,
                expected_version=0,
                weekdays=(0,),
                opens_at=time(8, 0),
                closes_at=time(18, 0),
                response_hours=4,
                responsible_roles=("nurse",),
            )
        with pytest.raises(WorkflowInputError):
            urgent.set_urgent_coverage(
                clinic_id=rbac_graph.clinic_a,
                expected_version=1,
                weekdays=(0,),
                opens_at=time(18, 0),
                closes_at=time(8, 0),
                response_hours=4,
                responsible_roles=("nurse",),
            )
    with runtime_role(), patient_session_context(bare.session):
        lines = patient_coverage_text()
    assert len(lines) == 2
    assert "08:00" in lines[0]
    assert "18:00" in lines[0]
    assert "192" in lines[1]
    assert "4" in lines[1]


def test_release_queues_the_approved_notice_without_result_content(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = release_world(rbac_graph, tmp_path)
    graph = world.graph
    with as_actor(graph, graph.physician):
        with pytest.raises(WorkflowConflictError):
            release_result_to_patient(
                clinic_id=graph.clinic_a,
                result_version_id=world.version,
                template_version=2,
                audience="patient",
            )
        view = release_result_to_patient(
            clinic_id=graph.clinic_a,
            result_version_id=world.version,
            template_version=1,
            audience="patient",
        )
        again = release_result_to_patient(
            clinic_id=graph.clinic_a,
            result_version_id=world.version,
            template_version=1,
            audience="patient",
        )
    assert (view.state, again.delivery_id) == ("pending", view.delivery_id)
    with owner_context(graph.organization_a):
        delivery = ReleaseDelivery.objects.get(pk=view.delivery_id)
        message = Message.objects.get(pk=_message(delivery))
    assert message.direction == "out"
    assert run_operation(graph, _message(delivery)) == "succeeded"
    with as_actor(graph, graph.physician):
        [delivered] = release_deliveries(
            clinic_id=graph.clinic_a, result_version_id=world.version
        )
    assert (delivered.state, delivered.template_version) == ("delivered", 1)
    # Only a clinician approves wording, and wording never carries identifiers.
    with as_actor(graph, graph.clinic_admin), pytest.raises(CurrentActorError):
        publish_release_notice(clinic_id=graph.clinic_a, body=NOTICE)
    with as_actor(graph, graph.physician), pytest.raises(WorkflowInputError):
        publish_release_notice(clinic_id=graph.clinic_a, body="Ligue 5555-0000.")


def test_guardian_access_change_re_evaluates_release_scope(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = release_world(rbac_graph, tmp_path)
    graph = world.graph
    with as_actor(graph, graph.physician):
        view = release_result_to_patient(
            clinic_id=graph.clinic_a,
            result_version_id=world.version,
            template_version=1,
            audience="delegate",
            delegate_grant_id=world.grant.grant_id,
        )
    assert view.audience == "delegate"
    # The guardian's access ends after the release but before the send.
    with permission_context(graph, world.delegate.verifier_id):
        revoke(
            clinic_id=graph.clinic_a,
            grant_id=world.grant.grant_id,
            expected_revision=world.grant.revision,
        )
    with owner_context(graph.organization_a):
        delivery = ReleaseDelivery.objects.get(pk=view.delivery_id)
    assert run_operation(graph, _message(delivery)) == "cancelled"
    with owner_context(graph.organization_a):
        delivery.refresh_from_db()
        operation = IntegrationOperation.objects.get(subject_id=_message(delivery))
        assert delivery.failure_task_id is not None
        task = Task.objects.get(pk=delivery.failure_task_id)
    assert (delivery.failure_reason, operation.status) == ("scope_revoked", "cancelled")
    _assert_follow_up(task, world.enrollment)
    with as_actor(graph, graph.physician):
        [failed] = release_deliveries(
            clinic_id=graph.clinic_a, result_version_id=world.version
        )
    assert failed.state == "failed"


def test_failed_notice_delivery_opens_one_follow_up_task(
    rbac_graph: RbacGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = release_world(rbac_graph, tmp_path)
    graph = world.graph
    with as_actor(graph, graph.physician):
        view = release_result_to_patient(
            clinic_id=graph.clinic_a,
            result_version_id=world.version,
            template_version=1,
            audience="patient",
        )
    with owner_context(graph.organization_a):
        delivery = ReleaseDelivery.objects.get(pk=view.delivery_id)
    # The portal provider is unavailable: the operation fails without a send.
    monkeypatch.delitem(integration._SEND_ADAPTERS, PROVIDER_BY_CHANNEL["portal"])
    assert run_operation(graph, _message(delivery)) == "failed"
    assert run_operation(graph, _message(delivery)) == "skipped"
    with owner_context(graph.organization_a):
        delivery.refresh_from_db()
        operation = IntegrationOperation.objects.get(subject_id=_message(delivery))
        tasks = list(Task.objects.filter(idempotency_key=delivery.pk))
    assert (operation.status, operation.attempt_count, operation.last_error) == (
        "failed",
        1,
        "adapter_unavailable",
    )
    assert delivery.failure_reason == "send_failed"
    assert [task.pk for task in tasks] == [delivery.failure_task_id]
    _assert_follow_up(tasks[0], world.enrollment)
    with as_actor(graph, graph.physician):
        [failed] = release_deliveries(
            clinic_id=graph.clinic_a, result_version_id=world.version
        )
    assert failed.state == "failed"
