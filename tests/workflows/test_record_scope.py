"""Record scope: a referenced or target row's clinic against the call's clinic.

Every derived record-scope site, in Python lookups (``decision_sites``) and in
SQL functions and trigger statements (``sql_sites``), has a case here with a
same-clinic record (allowed) and an another-clinic record (refused). Refusals
by the services must equal the unknown-id denial and write nothing.
"""

from __future__ import annotations

import ast
import copy
import inspect
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4, uuid5

import pytest
from apps.comms.adapters import OperationScope
from apps.core import integration
from apps.core.chart_sources import ChartBatch
from apps.core.integration import ActionOperationRequest, enqueue_operation
from apps.ehr.finalization import finalize_version
from apps.ehr.models import Encounter, SpecialtyTemplate
from apps.ehr.services import create_draft, open_encounter, record_clinical_note
from apps.identity.current_context import CurrentActorError
from apps.identity.models import CareTeamMembership, User, UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import patient_session_context
from apps.prescription.models import DocumentKind
from apps.prescription.services import create_document_draft
from apps.scheduling.availability_creation import create_availability
from apps.scheduling.models import Appointment
from apps.tenancy.db import tenant_context
from apps.workflows import (
    access,
    chart_sources,
    coordination_views,
    definition_services,
    designer,
    engine,
    external,
    handlers,
    receipts,
    release,
    run_control,
    run_views,
    task_feed,
    today,
    urgent,
    views,
)
from apps.workflows.care import plans, pro, recalls
from apps.workflows.care import services as care
from apps.workflows.care.models import (
    CareAction,
    CarePlan,
    PROAssignment,
    PROOccurrence,
    Recall,
    Referral,
)
from apps.workflows.care.validation import (
    CareActionSpec,
    PROSpec,
    ReferralSpec,
    return_window,
)
from apps.workflows.inbox import InboxScope
from apps.workflows.models import (
    Task,
    WorkflowDefinitionVersion,
    WorkflowRun,
    WorkflowStep,
)
from apps.workflows.reassignment import apply_reassignment, preview_reassignment
from apps.workflows.release_models import ReleaseNoticeTemplate
from apps.workflows.services import (
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
    assign_task,
    cancel_run,
    complete_task,
    create_task,
    list_tasks,
    publish_definition,
    start_run,
    start_task,
)
from apps.workflows.urgent_models import UrgentConcern
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.http import HttpResponse, QueryDict
from django.urls import resolve
from django.utils import timezone
from django.utils.translation import gettext

from auth.stepup_test_support import STEP_UP_NOW, verified_request
from ehr.test_addenda import clinician, enrollment_of, join
from ehr.test_autosave import SECTIONS, as_actor, enrollment, registration
from identity.permission_support import (
    owner_context,
    permission_context,
)
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    arrived_synthetic_appointment,
    create_synthetic_appointment,
    seed_cross_clinic_appointment_setups,
)
from workflows import decision_sites
from workflows.coordination_support import (
    URGENT,
    clinic_b_portal,
    patient_reply,
    reply_as,
    urgent_world,
)
from workflows.coordination_world import seed_release
from workflows.decision_sites import SCOPE, discover_sites
from workflows.permission_support import permission_actor
from workflows.sql_sites import discover_scope_conditions
from workflows.test_authority import _pending_writes

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.workflows.handlers import HandlerResult
    from django.http import HttpRequest

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("synthetic_workflow_transport"),
]
TIMER = [{"handler": "timer", "seconds": 0}]
KINDS = ("clinic", "enrollment", "appointment", "task")
REFUSALS = (CurrentActorError, WorkflowConflictError, WorkflowInputError)


@dataclass(frozen=True)
class Scoped:
    graph: RbacGraph
    staff: UUID
    manager: UUID
    references: dict[UUID, dict[str, dict[str, str]]]
    definitions: dict[UUID, UUID]

    @property
    def a(self) -> UUID:
        return self.graph.clinic_a

    @property
    def b(self) -> UUID:
        return self.graph.clinic_b

    def ref(self, clinic: UUID, kind: str) -> dict[str, str]:
        return self.references[clinic][kind]


def spec(clinic: UUID, subject: dict[str, str] | None = None) -> TaskSpec:
    return TaskSpec(
        kind="checklist",
        subject_ref=subject or {"kind": "clinic", "id": str(clinic)},
        due_at=timezone.now() + timedelta(hours=1),
    )


@pytest.fixture
def scoped(rbac_graph: RbacGraph) -> Scoped:
    graph = rbac_graph
    first, second = seed_cross_clinic_appointment_setups(graph)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(user_id=graph.shared_user).update(
            workflow_bundle_version=2
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointments = {
            graph.clinic_a: create_synthetic_appointment(first).pk,
            # Same synthetic patient in both clinics: book a later slot.
            graph.clinic_b: create_synthetic_appointment(
                second, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
            ).pk,
        }
        own = {
            clinic: create_task(
                clinic_id=clinic, spec=spec(clinic), idempotency_key=uuid4()
            ).pk
            for clinic in (graph.clinic_a, graph.clinic_b)
        }
    manager, _enrollment = permission_actor(graph, "clinic_manager")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=manager,
            role=UserClinicRole.Role.CLINIC_MANAGER,
            workflow_bundle_version=2,
        )
    with runtime_role(), tenant_context(manager, graph.organization_a):
        # Distinct keys: same-key publication across clinics is its own case.
        definitions = {
            clinic: publish_definition(
                clinic_id=clinic,
                key=f"scope-{index}",
                steps=TIMER,
                idempotency_key=uuid4(),
            ).pk
            for index, clinic in enumerate((graph.clinic_a, graph.clinic_b))
        }
    enrollments = {
        graph.clinic_a: first.enrollment_id,
        graph.clinic_b: second.enrollment_id,
    }
    references = {
        clinic: {
            "clinic": {"kind": "clinic", "id": str(clinic)},
            "enrollment": {"kind": "enrollment", "id": str(enrollments[clinic])},
            "appointment": {"kind": "appointment", "id": str(appointments[clinic])},
            "task": {"kind": "task", "id": str(own[clinic])},
        }
        for clinic in (graph.clinic_a, graph.clinic_b)
    }
    return Scoped(graph, graph.shared_user, manager, references, definitions)


def outcome(s: Scoped, actor: UUID, call: Callable[[], object]) -> tuple[object, bool]:
    """Run as the actor; return (result or refusal type+args, attempted writes)."""
    with permission_context(s.graph, actor):
        before = _pending_writes()
        try:
            result: object = call()
        except REFUSALS as error:
            result = (type(error), error.args)
        wrote = _pending_writes() != before
        transaction.set_rollback(True)
    return result, wrote


def same_denial(
    s: Scoped, actor: UUID, foreign: Callable[[], object], unknown: Callable[[], object]
) -> None:
    """Another clinic's record is refused like an unknown id and writes nothing."""
    refused, wrote = outcome(s, actor, foreign)
    baseline, baseline_wrote = outcome(s, actor, unknown)
    assert refused == baseline, (refused, baseline)
    assert isinstance(refused, tuple)
    assert issubclass(refused[0], CurrentActorError), refused
    assert not wrote
    assert not baseline_wrote


def unknown(kind: str) -> dict[str, str]:
    return {"kind": kind, "id": str(uuid4())}


# --- Python record-scope sites --------------------------------------------------


def reference(kind: str) -> Callable[[Scoped], None]:
    """require_reference, create_task, start_run and completion evidence."""

    def case(s: Scoped) -> None:
        foreign, mine = s.ref(s.b, kind), s.ref(s.a, kind)
        same_denial(
            s,
            s.staff,
            lambda: access.require_reference(clinic_id=s.a, value=foreign),
            lambda: access.require_reference(clinic_id=s.a, value=unknown(kind)),
        )
        assert (
            outcome(
                s, s.staff, lambda: access.require_reference(clinic_id=s.a, value=mine)
            )[0]
            == mine
        )
        same_denial(
            s,
            s.staff,
            lambda: create_task(
                clinic_id=s.a, spec=spec(s.a, foreign), idempotency_key=uuid4()
            ),
            lambda: create_task(
                clinic_id=s.a, spec=spec(s.a, unknown(kind)), idempotency_key=uuid4()
            ),
        )
        created, _wrote = outcome(
            s,
            s.staff,
            lambda: create_task(
                clinic_id=s.a, spec=spec(s.a, mine), idempotency_key=uuid4()
            ),
        )
        assert isinstance(created, Task)

        def run(refs: dict[str, str]) -> object:
            return start_run(
                clinic_id=s.a,
                definition_version_id=s.definitions[s.a],
                context_refs=[refs],
                idempotency_key=uuid4(),
            )

        same_denial(s, s.manager, lambda: run(foreign), lambda: run(unknown(kind)))
        assert isinstance(outcome(s, s.manager, lambda: run(mine))[0], WorkflowRun)

        def complete(record: dict[str, str]) -> object:
            task = create_task(
                clinic_id=s.a,
                spec=replace(spec(s.a), kind="review"),
                idempotency_key=uuid4(),
            )
            task = assign_task(
                clinic_id=s.a,
                task_id=task.pk,
                owner=TaskOwner(user_id=s.staff),
                expected_revision=1,
            )
            task = start_task(clinic_id=s.a, task_id=task.pk, expected_revision=2)
            return complete_task(
                clinic_id=s.a,
                task_id=task.pk,
                evidence={"record": record, "outcome": "reviewed"},
                expected_revision=3,
            )

        refused, _wrote = outcome(s, s.staff, lambda: complete(foreign))
        baseline, _wrote = outcome(s, s.staff, lambda: complete(unknown(kind)))
        assert refused == baseline
        assert isinstance(refused, tuple)
        assert refused[0] is access.WorkflowAccessDeniedError
        done = outcome(s, s.staff, lambda: complete(mine))[0]
        assert isinstance(done, Task)
        assert done.state == "done"

    return case


def task_scope(s: Scoped) -> None:
    foreign = UUID(s.ref(s.b, "task")["id"])
    same_denial(
        s,
        s.staff,
        lambda: access.require_task_access(
            clinic_id=s.a, task_id=foreign, permission="tasks.view"
        ),
        lambda: access.require_task_access(
            clinic_id=s.a, task_id=uuid4(), permission="tasks.view"
        ),
    )
    own = UUID(s.ref(s.a, "task")["id"])
    found = outcome(
        s,
        s.staff,
        lambda: access.require_task_access(
            clinic_id=s.a, task_id=own, permission="tasks.view"
        ),
    )[0]
    assert isinstance(found, Task)


def role_scope(s: Scoped) -> None:
    """A role held only in another clinic does not own this clinic's work."""
    member, _enrollment = permission_actor(s.graph, "receptionist")
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=member,
            role="nurse",
            workflow_bundle_version=2,
        )
    tasks: dict[UUID, Task] = {}
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        for clinic in (s.a, s.b):
            task = create_task(
                clinic_id=clinic, spec=spec(clinic), idempotency_key=uuid4()
            )
            tasks[clinic] = assign_task(
                clinic_id=clinic,
                task_id=task.pk,
                owner=TaskOwner(role="nurse"),
                expected_revision=1,
            )
    assert outcome(s, member, lambda: access.owner_matches(tasks[s.a]))[0] is False
    assert outcome(s, member, lambda: access.owner_matches(tasks[s.b]))[0] is True


def send_scope(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration, "_dispatch", lambda _operation: None)
    external.register_adapters()
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        definition = publish_definition(
            clinic_id=s.a,
            key="scope-send",
            steps=[{"handler": "external", "provider": "workflow-synthetic-v1"}],
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=s.a,
            definition_version_id=definition.pk,
            context_refs=[s.ref(s.a, "clinic")],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
    with runtime_role():
        assert engine.execute_step(step_id=step.pk) == "waiting"
    with owner_context(s.graph.organization_a):
        operation = WorkflowStep.objects.get(pk=step.pk).operation_id
    assert operation is not None

    def eligible(clinic: UUID) -> object:
        return external.step_send_eligible(
            OperationScope(
                operation_id=operation,
                organization_id=s.graph.organization_a,
                clinic_id=clinic,
                actor_id=s.manager,
            )
        )

    assert outcome(s, s.manager, lambda: eligible(s.b))[0] is False
    assert outcome(s, s.manager, lambda: eligible(s.a))[0] is True


def preview_scope(s: Scoped) -> None:
    foreign_task = UUID(s.ref(s.b, "task")["id"])
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        token = preview_reassignment(
            clinic_id=s.b, task_ids=[foreign_task], owner=TaskOwner(user_id=s.manager)
        ).token
    refused = outcome(
        s, s.manager, lambda: apply_reassignment(clinic_id=s.a, preview_token=token)
    )[0]
    assert isinstance(refused, tuple)
    assert refused[0] is WorkflowConflictError
    applied = outcome(
        s, s.manager, lambda: apply_reassignment(clinic_id=s.b, preview_token=token)
    )[0]
    assert isinstance(applied, tuple)
    assert isinstance(applied[0], Task)
    assert applied[0].owner_user_id == s.manager


def publication_scope(s: Scoped) -> None:
    key = uuid4()
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        publish_definition(
            clinic_id=s.b, key="shared", steps=TIMER, idempotency_key=key
        )
        publish_definition(
            clinic_id=s.b, key="shared", steps=TIMER, idempotency_key=uuid4()
        )
        mine = publish_definition(
            clinic_id=s.a, key="shared", steps=TIMER, idempotency_key=key
        )
    assert mine.clinic_id == s.a
    assert mine.version == 1


def run_scope(s: Scoped) -> None:
    def start(clinic: UUID, definition: UUID, key: UUID) -> object:
        return start_run(
            clinic_id=clinic,
            definition_version_id=definition,
            context_refs=[s.ref(clinic, "clinic")],
            idempotency_key=key,
        )

    same_denial(
        s,
        s.manager,
        lambda: start(s.a, s.definitions[s.b], uuid4()),
        lambda: start(s.a, uuid4(), uuid4()),
    )
    key = uuid4()
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        start(s.b, s.definitions[s.b], key)
        mine = start(s.a, s.definitions[s.a], key)
    assert isinstance(mine, WorkflowRun)
    assert mine.clinic_id == s.a


def cancel_scope(s: Scoped) -> None:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        runs = {
            clinic: start_run(
                clinic_id=clinic,
                definition_version_id=s.definitions[clinic],
                context_refs=[s.ref(clinic, "clinic")],
                idempotency_key=uuid4(),
            ).pk
            for clinic in (s.a, s.b)
        }
    same_denial(
        s,
        s.manager,
        lambda: cancel_run(clinic_id=s.a, run_id=runs[s.b]),
        lambda: cancel_run(clinic_id=s.a, run_id=uuid4()),
    )
    cancelled = outcome(
        s, s.manager, lambda: cancel_run(clinic_id=s.a, run_id=runs[s.a])
    )[0]
    assert isinstance(cancelled, WorkflowRun)
    assert cancelled.state == "cancelled"


def task_key_scope(s: Scoped) -> None:
    key = uuid4()
    with runtime_role(), tenant_context(s.staff, s.graph.organization_a):
        create_task(clinic_id=s.b, spec=spec(s.b), idempotency_key=key)
        mine = create_task(clinic_id=s.a, spec=spec(s.a), idempotency_key=key)
    assert mine.clinic_id == s.a


def queue_scope(s: Scoped) -> None:
    listed = outcome(s, s.staff, lambda: list_tasks(clinic_id=s.a))[0]
    assert isinstance(listed, tuple)
    ids = {task.pk for task in listed}
    assert UUID(s.ref(s.a, "task")["id"]) in ids
    assert UUID(s.ref(s.b, "task")["id"]) not in ids


def inbox_task_scope(s: Scoped) -> None:
    """The inbox task feed lists the actor's own work of the queried clinic only."""

    def keys(clinic: UUID) -> set[str]:
        scope = InboxScope(
            clinic_id=clinic,
            actor_id=s.staff,
            timezone="America/Sao_Paulo",
            now=timezone.now(),
        )
        listed = outcome(s, s.staff, lambda: task_feed.task_items(scope))[0]
        assert isinstance(listed, tuple)
        return {item.key for item in listed}

    own, foreign = s.ref(s.a, "task")["id"], s.ref(s.b, "task")["id"]
    assert f"task:{own}" in keys(s.a)
    assert f"task:{foreign}" not in keys(s.a)
    assert f"task:{foreign}" in keys(s.b)


def today_scope(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    """A physician of two clinics sees only the queried clinic's own day."""
    physician = s.graph.physician
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=physician,
            role=UserClinicRole.Role.PHYSICIAN,
            workflow_bundle_version=2,
        )
        mine = Appointment.objects.get(pk=s.ref(s.a, "appointment")["id"])
    with runtime_role(), tenant_context(s.staff, s.graph.organization_a):
        create_availability(
            clinic_id=s.b,
            practitioner_id=physician,
            start_local="2035-06-02T13:00",
            end_local="2035-06-02T15:00",
            idempotency_key=uuid4(),
        )
        elsewhere = create_synthetic_appointment(
            AppointmentSetup(
                organization_id=s.graph.organization_a,
                clinic_id=s.b,
                actor_id=s.staff,
                practitioner_id=physician,
                enrollment_id=UUID(s.ref(s.b, "enrollment")["id"]),
                patient_id=mine.patient_id,
            ),
            start_local="2035-06-02T13:00",
            end_local="2035-06-02T14:00",
        )
    monkeypatch.setattr(
        today, "utc_now", lambda: datetime(2035, 6, 2, 11, 30, tzinfo=UTC)
    )
    for clinic, expected in ((s.a, mine.pk), (s.b, elsewhere.pk)):
        screen = outcome(s, physician, partial(today.view_today, clinic_id=clinic))[0]
        assert isinstance(screen, today.TodayView), screen
        assert [row.appointment_id for row in screen.appointments] == [expected]
        assert [row.enrollment_id for row in screen.appointments] == [
            UUID(s.ref(clinic, "enrollment")["id"])
        ]


def chart_task_scope(s: Scoped) -> None:
    """The same patient's two clinic enrollments never mix tasks or appointments."""
    physician = s.graph.physician
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.filter(user_id=physician, clinic_id=s.a).update(
            workflow_bundle_version=2
        )
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=physician,
            role=UserClinicRole.Role.PHYSICIAN,
            workflow_bundle_version=2,
        )
    for clinic in (s.a, s.b):
        registration(s.graph, physician, clinic)
    with owner_context(s.graph.organization_a):
        for clinic in (s.a, s.b):
            CareTeamMembership.objects.create(
                organization_id=s.graph.organization_a,
                clinic_id=clinic,
                patient_enrollment_id=s.ref(clinic, "enrollment")["id"],
                user_id=physician,
                role=UserClinicRole.Role.PHYSICIAN,
                valid_from=timezone.now() - timedelta(days=1),
            )
    expected: dict[UUID, set[UUID]] = {}
    with permission_context(s.graph, physician):
        for clinic in (s.a, s.b):
            expected[clinic] = {
                create_task(
                    clinic_id=clinic,
                    spec=spec(clinic, s.ref(clinic, kind)),
                    idempotency_key=uuid4(),
                ).pk
                for kind in ("enrollment", "appointment")
            }
            # Visible operational work is not automatically this patient's work.
            create_task(clinic_id=clinic, spec=spec(clinic), idempotency_key=uuid4())
    for clinic, foreign in ((s.a, s.b), (s.b, s.a)):
        batch = outcome(
            s,
            physician,
            partial(
                chart_sources.read_task_sources,
                clinic_id=clinic,
                enrollment_id=UUID(s.ref(clinic, "enrollment")["id"]),
            ),
        )[0]
        assert isinstance(batch, ChartBatch), batch
        assert {source.id for source in batch.sources} == expected[clinic]
        same_denial(
            s,
            physician,
            partial(
                chart_sources.read_task_sources,
                clinic_id=clinic,
                enrollment_id=UUID(s.ref(foreign, "enrollment")["id"]),
            ),
            partial(
                chart_sources.read_task_sources, clinic_id=clinic, enrollment_id=uuid4()
            ),
        )


def exceptions_scope(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        for clinic in (s.a, s.b):
            start_run(
                clinic_id=clinic,
                definition_version_id=s.definitions[clinic],
                context_refs=[s.ref(clinic, "clinic")],
                idempotency_key=uuid4(),
            )
    request = verified_request(s.manager, verified_at=STEP_UP_NOW)
    request.method = "GET"
    request.path = request.path_info = f"/clinics/{s.a}/tasks/exceptions/"
    request.resolver_match = resolve(request.path)
    request.META.update(SERVER_NAME="testserver", SERVER_PORT="80")
    later = timezone.now() + timedelta(hours=1)
    # Both pending runs are stale an hour later; the GET writes nothing.
    monkeypatch.setattr(timezone, "now", lambda: later)
    response = outcome(
        s,
        s.manager,
        lambda: inspect.unwrap(views._authorized_tasks)(request, s.a, exceptions=True),
    )[0]
    content = getattr(response, "content", b"")
    assert content.count(b"data-run-state") == 1, content.count(b"data-run-state")


def definition_scope(s: Scoped) -> None:
    same_denial(
        s,
        s.manager,
        lambda: definition_services._definition(
            clinic_id=s.a, definition_version_id=s.definitions[s.b]
        ),
        lambda: definition_services._definition(
            clinic_id=s.a, definition_version_id=uuid4()
        ),
    )
    definition = outcome(
        s,
        s.manager,
        lambda: definition_services._definition(
            clinic_id=s.a, definition_version_id=s.definitions[s.a]
        ),
    )[0]
    assert isinstance(definition, WorkflowDefinitionVersion)
    assert definition.pk == s.definitions[s.a]


def designer_command_scope(s: Scoped) -> None:
    request = verified_request(s.manager)
    request.method = "POST"

    def retire(identifier: UUID) -> object:
        request.POST = QueryDict(
            urlencode(
                {
                    "action": "retire",
                    "definition_id": str(identifier),
                    "expected_revision": "3",
                    "idempotency_key": str(uuid4()),
                }
            )
        )
        command: Callable[[HttpRequest, UUID], object] = designer._command
        return command(request, s.a)

    same_denial(
        s, s.manager, lambda: retire(s.definitions[s.b]), lambda: retire(uuid4())
    )
    assert outcome(s, s.manager, lambda: retire(s.definitions[s.a]))[0] is None


def designer_queue_scope(s: Scoped) -> None:
    request = verified_request(s.manager)
    request.method = "GET"
    response = outcome(
        s,
        s.manager,
        lambda: inspect.unwrap(designer._authorized_designer)(request, s.a),
    )[0]
    content = getattr(response, "content", b"")
    assert f'data-definition="{s.definitions[s.a]}"'.encode() in content
    assert f'data-definition="{s.definitions[s.b]}"'.encode() not in content


def native_run_scope(s: Scoped) -> None:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        runs = {
            clinic: start_run(
                clinic_id=clinic,
                definition_version_id=s.definitions[clinic],
                context_refs=[s.ref(clinic, "clinic")],
                idempotency_key=uuid4(),
            ).pk
            for clinic in (s.a, s.b)
        }
    request = verified_request(s.manager)
    request.method = "GET"
    response = outcome(
        s,
        s.manager,
        lambda: inspect.unwrap(run_views._authorized_runs)(request, s.a),
    )[0]
    content = getattr(response, "content", b"")
    assert f'data-run="{runs[s.a]}"'.encode() in content
    assert f'data-run="{runs[s.b]}"'.encode() not in content
    assert str(s.definitions[s.a]).encode() in content
    assert str(s.definitions[s.b]).encode() not in content
    request.method = "POST"

    def cancel(identifier: UUID) -> object:
        request.POST = QueryDict(
            urlencode(
                {
                    "action": "cancel",
                    "run_id": str(identifier),
                    "expected_revision": "1",
                }
            )
        )
        command: Callable[[HttpRequest, UUID], object] = run_views._command
        return command(request, s.a)

    same_denial(s, s.manager, lambda: cancel(runs[s.b]), lambda: cancel(uuid4()))
    assert outcome(s, s.manager, lambda: cancel(runs[s.a]))[0] is None


def retry_scope(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    def rejected(_step: WorkflowStep, _now: datetime) -> Literal["completed"]:
        return "completed"

    runs = {}
    with monkeypatch.context() as broken:
        broken.setitem(engine.HANDLERS, "timer", rejected)
        for clinic in (s.a, s.b):
            with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
                run = start_run(
                    clinic_id=clinic,
                    definition_version_id=s.definitions[clinic],
                    context_refs=[s.ref(clinic, "clinic")],
                    idempotency_key=uuid4(),
                )
                step = WorkflowStep.objects.get(run=run)
            with runtime_role():
                assert engine.execute_step(step_id=step.pk) == "failed"
            with owner_context(s.graph.organization_a):
                run.refresh_from_db()
                runs[clinic] = run

    def retry(identifier: UUID) -> object:
        return run_control.retry_run(
            clinic_id=s.a,
            run_id=identifier,
            expected_revision=runs[s.a].revision,
            idempotency_key=uuid4(),
        )

    same_denial(s, s.manager, lambda: retry(runs[s.b].pk), lambda: retry(uuid4()))
    retried = outcome(s, s.manager, lambda: retry(runs[s.a].pk))[0]
    assert isinstance(retried, WorkflowRun)
    assert retried.state == "pending"


def receipt_scope(s: Scoped) -> None:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        definition = publish_definition(
            clinic_id=s.a,
            key="scope-task-receipt",
            steps=[
                {
                    "handler": "task",
                    "kind": "checklist",
                    "subject": 0,
                    "owner_role": "clinic_manager",
                    "due_seconds": 3600,
                }
            ],
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=s.a,
            definition_version_id=definition.pk,
            context_refs=[s.ref(s.a, "clinic")],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.select_related("run__definition_version").get(
            run=run
        )
        task = create_task(clinic_id=s.a, spec=spec(s.a), idempotency_key=step.pk)
        task = assign_task(
            clinic_id=s.a,
            task_id=task.pk,
            owner=TaskOwner(user_id=s.manager),
            expected_revision=1,
        )
        start_task(clinic_id=s.a, task_id=task.pk, expected_revision=2)
        complete_task(
            clinic_id=s.a,
            task_id=task.pk,
            expected_revision=3,
            evidence={"checked": True},
        )
        foreign = create_task(clinic_id=s.b, spec=spec(s.b), idempotency_key=step.pk)

    def receipt(identifier: UUID) -> object:
        candidate = copy.copy(step)
        candidate.created_task_id = identifier
        validate: Callable[[WorkflowStep, HandlerResult, datetime], object] = (
            receipts.validate_step_receipt
        )
        return validate(candidate, "completed", timezone.now())

    denied = outcome(s, s.manager, lambda: receipt(foreign.pk))
    unknown_denied = outcome(s, s.manager, lambda: receipt(uuid4()))
    assert denied == unknown_denied
    assert isinstance(denied[0], tuple)
    assert denied[0][0] is WorkflowInputError
    assert denied[1] is False
    assert outcome(s, s.manager, lambda: receipt(task.pk))[0] is None


def external_receipt_scope(s: Scoped) -> None:
    steps = {}
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        for clinic in (s.a, s.b):
            definition = publish_definition(
                clinic_id=clinic,
                key="scope-external-receipt",
                steps=[{"handler": "external", "provider": "workflow-synthetic-v1"}],
                idempotency_key=uuid4(),
            )
            run = start_run(
                clinic_id=clinic,
                definition_version_id=definition.pk,
                context_refs=[s.ref(clinic, "clinic")],
                idempotency_key=uuid4(),
            )
            step = WorkflowStep.objects.select_related("run__definition_version").get(
                run=run
            )
            assert handlers._external(step, timezone.now()) == "waiting"
            steps[clinic] = step

    def receipt(identifier: UUID) -> object:
        candidate = copy.copy(steps[s.a])
        candidate.operation_id = identifier
        validate: Callable[[WorkflowStep, HandlerResult, datetime], object] = (
            receipts.validate_step_receipt
        )
        return validate(candidate, "waiting", timezone.now())

    foreign_id = steps[s.b].operation_id
    own_id = steps[s.a].operation_id
    assert foreign_id is not None
    assert own_id is not None
    denied = outcome(s, s.manager, lambda: receipt(foreign_id))
    unknown_denied = outcome(s, s.manager, lambda: receipt(uuid4()))
    assert denied == unknown_denied
    assert isinstance(denied[0], tuple)
    assert denied[0][0] is WorkflowInputError
    assert denied[1] is False
    assert outcome(s, s.manager, lambda: receipt(own_id))[0] is None


# --- Care record-scope sites (todo 52) -------------------------------------------
# The clinician is on one patient's care team in each clinic and registered in
# both; the manager manages both. Every permission check passes for the call's
# clinic, so only each lookup's clinic filter refuses the other clinic's record.

CALM = {f"item_{n}": "0" for n in range(1, 10)}


@dataclass(frozen=True)
class CareSide:
    """One clinic's care records on one patient of the dual-clinic clinician."""

    enrollment: UUID
    encounter: UUID
    plan: UUID
    plan_key: UUID
    window: UUID
    overdue: UUID
    referral: UUID
    draft: UUID


def care_clinician(s: Scoped) -> tuple[UUID, dict[UUID, UUID]]:
    enrollments = {clinic: enrollment(s.graph, clinic) for clinic in (s.a, s.b)}
    user = clinician(s.graph, "physician", enrollments[s.a], s.a)
    join(s.graph, user, "physician", s.b, enrollments[s.b])
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.filter(user_id=user).update(workflow_bundle_version=2)
    return user, enrollments


def scheduled_encounter(
    s: Scoped, author: UUID, clinic: UUID, enrolled: UUID, hour: int
) -> UUID:
    """An encounter on the author's arrived appointment (referral documents)."""
    with owner_context(s.graph.organization_a):
        patient = PatientClinicEnrollment.objects.get(pk=enrolled).patient_id
    with runtime_role(), tenant_context(s.staff, s.graph.organization_a):
        # One slot per clinic: a practitioner's availability never overlaps
        # across clinics (scheduling_availability_active_practitioner_excl).
        create_availability(
            clinic_id=clinic,
            practitioner_id=author,
            start_local=f"2035-06-05T{hour:02d}:00",
            end_local=f"2035-06-05T{hour + 1:02d}:00",
            idempotency_key=uuid4(),
        )
        appointment = arrived_synthetic_appointment(
            AppointmentSetup(
                organization_id=s.graph.organization_a,
                clinic_id=clinic,
                actor_id=s.staff,
                practitioner_id=author,
                enrollment_id=enrolled,
                patient_id=patient,
            ),
            start_local=f"2035-06-05T{hour:02d}:00",
            end_local=f"2035-06-05T{hour + 1:02d}:00",
        )
    with as_actor(s.graph, author):
        return open_encounter(clinic_id=clinic, appointment_id=appointment.pk).pk


def finalized_note(
    s: Scoped, author: UUID, clinic: UUID, enrolled: UUID, hour: int | None = None
) -> tuple[UUID, UUID]:
    """A finalized SOAP version on its own encounter in ``clinic``.

    With ``hour`` the encounter is the author's arrived appointment.
    """
    with owner_context(s.graph.organization_a):
        patient = PatientClinicEnrollment.objects.get(pk=enrolled).patient_id
        template = SpecialtyTemplate.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=clinic,
            key=f"care-scope-{uuid4().hex[:12]}",
            version=1,
            title="Retorno sintetico",
            prompts=dict.fromkeys(
                ("subjective", "objective", "assessment", "plan"), "Campo sintetico"
            ),
        )
        if hour is None:
            encounter = Encounter.objects.create(
                organization_id=s.graph.organization_a,
                clinic_id=clinic,
                patient_id=patient,
                physician_id=author,
                unscheduled_reason="documentation_only",
            ).pk
    if hour is not None:
        encounter = scheduled_encounter(s, author, clinic, enrolled, hour)
    with as_actor(s.graph, author):
        draft = create_draft(
            clinic_id=clinic, encounter_id=encounter, template_id=template.pk
        )
    request = verified_request(author)
    with as_actor(s.graph, author):
        saved = record_clinical_note(
            clinic_id=clinic,
            version_id=draft.pk,
            expected_revision=draft.revision,
            content=SECTIONS,
        )
        version = finalize_version(
            clinic_id=clinic,
            version_id=draft.pk,
            expected_revision=saved.revision,
            request=request,
        )
    return version.pk, encounter


def care_side(s: Scoped, author: UUID, clinic: UUID, enrolled: UUID) -> CareSide:
    # Referral documents are created only from an arrived appointment.
    version, encounter = finalized_note(
        s, author, clinic, enrolled, hour=9 if clinic == s.a else 11
    )
    now = timezone.now()
    start, end = return_window(now=now, weeks=6)
    key = uuid4()
    with permission_context(s.graph, author):
        plan = care.create_care_plan_from_version(
            clinic_id=clinic,
            version_id=version,
            actions=(
                CareActionSpec(
                    kind="return_window", window_start=start, window_end=end
                ),
                CareActionSpec(
                    kind="staff_coordination",
                    window_start=now - timedelta(days=3),
                    window_end=now - timedelta(days=1),
                ),
                CareActionSpec(
                    kind="referral",
                    window_start=now,
                    window_end=now + timedelta(days=14),
                    referral=ReferralSpec(
                        target_kind="specialty", target_label="Cardiologia"
                    ),
                ),
            ),
            idempotency_key=key,
        )
    with as_actor(s.graph, author):
        draft = create_document_draft(
            clinic_id=clinic, encounter_id=encounter, kind=DocumentKind.REFERRAL
        )
    with owner_context(s.graph.organization_a):
        kinds = {a.kind: a.pk for a in CareAction.objects.filter(plan_id=plan.pk)}
        referral = Referral.objects.get(plan_id=plan.pk).pk
    return CareSide(
        enrollment=enrolled,
        encounter=encounter,
        plan=plan.pk,
        plan_key=key,
        window=kinds["return_window"],
        overdue=kinds["staff_coordination"],
        referral=referral,
        draft=draft.pk,
    )


def care_sides(s: Scoped) -> tuple[UUID, CareSide, CareSide]:
    author, enrollments = care_clinician(s)
    return (
        author,
        care_side(s, author, s.a, enrollments[s.a]),
        care_side(s, author, s.b, enrollments[s.b]),
    )


def care_plan_scope(s: Scoped) -> None:
    """load_plan, load_action, the stale and overdue transitions and both keys."""
    author, mine, theirs = care_sides(s)

    def cancel(action: UUID) -> object:
        return care.cancel_care_action(
            clinic_id=s.a, action_id=action, expected_revision=1
        )

    same_denial(s, s.manager, lambda: cancel(theirs.window), lambda: cancel(uuid4()))
    cancelled = outcome(s, s.manager, lambda: cancel(mine.window))[0]
    assert isinstance(cancelled, CareAction)
    assert cancelled.state == "cancelled"

    def add(plan: UUID, key: UUID | None = None) -> object:
        start, end = return_window(now=timezone.now(), weeks=8)
        return care.add_care_action(
            clinic_id=s.a,
            plan_id=plan,
            spec=CareActionSpec(
                kind="return_window", window_start=start, window_end=end
            ),
            idempotency_key=key or uuid4(),
        )

    same_denial(s, s.manager, lambda: add(theirs.plan), lambda: add(uuid4()))
    assert isinstance(outcome(s, s.manager, lambda: add(mine.plan))[0], CareAction)
    # Another clinic's replay key names a different command in this clinic.
    reused = uuid5(theirs.plan_key, "action:0")
    added = outcome(s, s.manager, lambda: add(mine.plan, reused))[0]
    assert isinstance(added, CareAction)
    assert (added.clinic_id, added.plan_id) == (s.a, mine.plan)

    def flag(plan: UUID) -> object:
        return plans.flag_stale_care_plan(clinic_id=s.a, plan_id=plan)

    same_denial(s, s.manager, lambda: flag(theirs.plan), lambda: flag(uuid4()))
    assert outcome(s, s.manager, lambda: flag(mine.plan))[0] is False

    def overdue(action: UUID) -> object:
        return plans.mark_care_action_overdue(clinic_id=s.a, action_id=action)

    same_denial(s, s.manager, lambda: overdue(theirs.overdue), lambda: overdue(uuid4()))
    assert outcome(s, s.manager, lambda: overdue(mine.overdue))[0] is True
    spare, _encounter = finalized_note(s, author, s.a, mine.enrollment)
    start, end = return_window(now=timezone.now(), weeks=6)
    replanned = outcome(
        s,
        author,
        lambda: care.create_care_plan_from_version(
            clinic_id=s.a,
            version_id=spare,
            actions=(
                CareActionSpec(
                    kind="return_window", window_start=start, window_end=end
                ),
            ),
            idempotency_key=theirs.plan_key,
        ),
    )[0]
    assert isinstance(replanned, CarePlan)
    assert (replanned.clinic_id, replanned.source_version_id) == (s.a, spare)


def pro_spec(plan: UUID | None = None) -> PROSpec:
    return PROSpec(
        instrument_key="phq9",
        first_due_at=timezone.now() + timedelta(days=7),
        interval_days=7,
        occurrences=1,
        response_role="physician",
        staffing_policy_ref="eg3-pending-staffing-policy",
        plan_id=plan,
    )


def care_pro_scope(s: Scoped) -> None:
    """assign_pro's enrollment, plan and replay lookups and both slot lookups."""
    author, mine, theirs = care_sides(s)

    def assign(
        enrolled: UUID,
        plan: UUID | None = None,
        key: UUID | None = None,
        clinic: UUID | None = None,
    ) -> PROAssignment:
        return care.assign_pro(
            clinic_id=clinic or s.a,
            enrollment_id=enrolled,
            spec=pro_spec(plan),
            idempotency_key=key or uuid4(),
        )

    same_denial(s, author, lambda: assign(theirs.enrollment), lambda: assign(uuid4()))
    own = outcome(s, author, lambda: assign(mine.enrollment))[0]
    assert isinstance(own, PROAssignment)
    same_denial(
        s,
        author,
        lambda: assign(mine.enrollment, theirs.plan),
        lambda: assign(mine.enrollment, uuid4()),
    )
    planned = outcome(s, author, lambda: assign(mine.enrollment, mine.plan))[0]
    assert isinstance(planned, PROAssignment)
    assert planned.plan_id == mine.plan
    key = uuid4()
    with permission_context(s.graph, author):
        foreign = assign(theirs.enrollment, key=key, clinic=s.b)
        local = assign(mine.enrollment)
    reused = outcome(s, author, lambda: assign(mine.enrollment, key=key))[0]
    assert isinstance(reused, PROAssignment)
    assert reused.clinic_id == s.a
    with owner_context(s.graph.organization_a):
        slot = {
            series.clinic_id: PROOccurrence.objects.get(assignment_id=series.pk).pk
            for series in (foreign, local)
        }

    def respond(occurrence: UUID) -> object:
        return care.record_pro_response(
            clinic_id=s.a, occurrence_id=occurrence, answers=CALM, expected_revision=1
        )

    same_denial(s, author, lambda: respond(slot[s.b]), lambda: respond(uuid4()))
    responded = outcome(s, author, lambda: respond(slot[s.a]))[0]
    assert isinstance(responded, PROOccurrence)
    assert responded.state == "responded"

    def close(occurrence: UUID) -> object:
        return pro.close_missed_pro_occurrence(clinic_id=s.a, occurrence_id=occurrence)

    same_denial(s, author, lambda: close(slot[s.b]), lambda: close(uuid4()))
    assert outcome(s, author, lambda: close(slot[s.a]))[0] is False


def care_recall_scope(s: Scoped) -> None:
    """schedule_recall's enrollment and replay lookups and materialization."""
    enrollments = {clinic: enrollment(s.graph, clinic) for clinic in (s.a, s.b)}

    def schedule(
        enrolled: UUID, key: UUID | None = None, clinic: UUID | None = None
    ) -> Recall:
        return care.schedule_recall(
            clinic_id=clinic or s.a,
            enrollment_id=enrolled,
            reason="routine_return",
            interval_days=30,
            owner_role="receptionist",
            idempotency_key=key or uuid4(),
        )

    same_denial(
        s, s.manager, lambda: schedule(enrollments[s.b]), lambda: schedule(uuid4())
    )
    assert isinstance(
        outcome(s, s.manager, lambda: schedule(enrollments[s.a]))[0], Recall
    )
    key = uuid4()
    with permission_context(s.graph, s.manager):
        foreign = schedule(enrollments[s.b], key, s.b)
        local = schedule(enrollments[s.a])
    reused = outcome(s, s.manager, lambda: schedule(enrollments[s.a], key))[0]
    assert isinstance(reused, Recall)
    assert reused.clinic_id == s.a
    assert reused.pk != foreign.pk

    def materialize(recall: UUID) -> object:
        return recalls.materialize_recall(clinic_id=s.a, recall_id=recall)

    same_denial(
        s, s.manager, lambda: materialize(foreign.pk), lambda: materialize(uuid4())
    )
    assert outcome(s, s.manager, lambda: materialize(local.pk))[0] is False


def care_referral_scope(s: Scoped) -> None:
    """The referral lookup and link_referral_document's plan and draft lookups."""
    author, mine, theirs = care_sides(s)

    def advance(referral: UUID) -> object:
        return care.advance_referral(
            clinic_id=s.a, referral_id=referral, status="scheduled", expected_revision=1
        )

    same_denial(s, author, lambda: advance(theirs.referral), lambda: advance(uuid4()))
    advanced = outcome(s, author, lambda: advance(mine.referral))[0]
    assert isinstance(advanced, Referral)
    assert advanced.status == "scheduled"

    def link(referral: UUID, draft: UUID) -> object:
        return care.link_referral_document(
            clinic_id=s.a, referral_id=referral, draft_id=draft, expected_revision=1
        )

    same_denial(
        s,
        author,
        lambda: link(theirs.referral, mine.draft),
        lambda: link(uuid4(), mine.draft),
    )
    same_denial(
        s,
        author,
        lambda: link(mine.referral, theirs.draft),
        lambda: link(mine.referral, uuid4()),
    )
    linked = outcome(s, author, lambda: link(mine.referral, mine.draft))[0]
    assert isinstance(linked, Referral)
    assert linked.document_id == mine.draft


# --- Todo 55: release notices, deliveries, coverage and urgent concerns ----------


def _role_in_b(s: Scoped, user: UUID, role: str) -> None:
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=user,
            role=role,
            workflow_bundle_version=2,
        )


def notice_scope(s: Scoped) -> None:
    """Notices are numbered, current and listed within their own clinic."""
    physician = s.graph.physician
    _role_in_b(s, physician, UserClinicRole.Role.PHYSICIAN)
    with as_actor(s.graph, physician):
        for body in ("Aviso da outra clinica.", "Aviso revisto da outra clinica."):
            release.publish_release_notice(clinic_id=s.b, body=body)
        release.publish_release_notice(clinic_id=s.a, body="Aviso desta clinica.")
    current = outcome(s, physician, lambda: release.current_notice(clinic_id=s.a))[0]
    assert isinstance(current, ReleaseNoticeTemplate)
    assert (current.version, current.body) == (1, "Aviso desta clinica.")
    # The verified request is built outside the runtime role that runs the page.
    request = verified_request(physician)
    page = outcome(
        s,
        physician,
        lambda: inspect.unwrap(coordination_views._release_notices)(request, s.a),
    )[0]
    assert isinstance(page, HttpResponse)
    assert 'data-notice-version="1"' in page.content.decode()
    assert 'data-notice-version="2"' not in page.content.decode()
    published = outcome(
        s,
        physician,
        lambda: release.publish_release_notice(
            clinic_id=s.a, body="Aviso desta clinica, revisto."
        ),
    )[0]
    assert isinstance(published, ReleaseNoticeTemplate)
    assert published.version == 2


def release_scope(s: Scoped) -> None:
    """A release and its deliveries resolve the version in the call's clinic."""
    physician = s.graph.physician
    # Released records without an appointment: the scope world already holds
    # the physician's availability.
    version = seed_release(s.graph, physician, s.a).version
    _role_in_b(s, physician, UserClinicRole.Role.PHYSICIAN)

    def release_in(clinic: UUID, target: UUID) -> Callable[[], object]:
        return lambda: release.release_result_to_patient(
            clinic_id=clinic,
            result_version_id=target,
            template_version=1,
            audience="patient",
        )

    same_denial(s, physician, release_in(s.b, version), release_in(s.b, uuid4()))
    with as_actor(s.graph, physician):
        view = release_in(s.a, version)()
    assert isinstance(view, release.DeliveryView)

    def deliveries(clinic: UUID) -> object:
        return outcome(
            s,
            physician,
            lambda: release.release_deliveries(
                clinic_id=clinic, result_version_id=version
            ),
        )[0]

    assert deliveries(s.a) == (view,)
    assert deliveries(s.b) == ()


def coverage_scope(s: Scoped) -> None:
    """Coverage versions and terms belong to their own clinic."""

    def publish(clinic: UUID, version: int, hours: int) -> Callable[[], object]:
        return lambda: urgent.set_urgent_coverage(
            clinic_id=clinic,
            expected_version=version,
            weekdays=(0, 1, 2, 3, 4),
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            response_hours=hours,
            responsible_roles=("nurse",),
        )

    with as_actor(s.graph, s.manager):
        publish(s.b, 0, 5)()
        publish(s.b, 1, 6)()
        publish(s.a, 0, 3)()
    current = outcome(s, s.manager, lambda: urgent.current_coverage(clinic_id=s.a))[0]
    assert isinstance(current, urgent.Coverage)
    assert (current.version, current.response_hours) == (1, 3)
    settings = outcome(s, s.manager, lambda: urgent.coverage_settings(clinic_id=s.b))[0]
    assert isinstance(settings, urgent.Coverage)
    assert settings.response_hours == 6
    published = outcome(s, s.manager, publish(s.a, 1, 4))[0]
    assert isinstance(published, urgent.Coverage)
    assert published.version == 2
    stale = outcome(s, s.manager, publish(s.a, 2, 4))[0]
    assert stale == (WorkflowConflictError, WorkflowConflictError().args)


def concern_scope(s: Scoped) -> None:
    """Concerns are listed and decided only within their own clinic."""
    world = urgent_world(s.graph, coverage=False)
    patient_reply(world, URGENT)
    session, thread = clinic_b_portal(s.graph, world.other_enrollment)
    reply_as(session, s.b, thread, URGENT)
    with owner_context(s.graph.organization_a):
        concerns = {row.clinic_id: row.pk for row in UrgentConcern.objects.all()}
    listed = outcome(s, s.manager, lambda: urgent.urgent_concerns(clinic_id=s.a))[0]
    assert isinstance(listed, tuple)
    assert [row.concern_id for row in listed] == [concerns[s.a]]

    def acknowledge(target: UUID) -> Callable[[], object]:
        return lambda: urgent.acknowledge_urgent_concern(
            clinic_id=s.a, concern_id=target, expected_revision=1
        )

    same_denial(s, s.manager, acknowledge(concerns[s.b]), acknowledge(uuid4()))
    acknowledged_row = outcome(s, s.manager, acknowledge(concerns[s.a]))[0]
    assert isinstance(acknowledged_row, urgent.ConcernRow)
    assert acknowledged_row.state == "acknowledged"


PYTHON = "apps.workflows."
CARE = PYTHON + "care."
PREVIEW = (
    PYTHON + "reassignment.apply_reassignment:scope:payload['clinic'] != str(clinic_id)"
)
PY_CASES: dict[str, Callable[..., None]] = {
    PYTHON + "access.require_task_access:scope:clinic_id=clinic_id": task_scope,
    PYTHON + "access.owner_matches:scope:clinic_id=task.clinic_id": role_scope,
    PYTHON + "access.require_reference:scope:identifier == clinic_id": reference(
        "clinic"
    ),
    PYTHON + "access.require_reference:scope:clinic_id=clinic_id": reference(
        "enrollment"
    ),
    PYTHON + "access.require_reference:scope:clinic_id=clinic_id#2": reference(
        "appointment"
    ),
    PYTHON + "access.require_reference:scope:clinic_id=clinic_id#3": reference("task"),
    PYTHON + "external.step_send_eligible:scope:clinic_id=scope.clinic_id": send_scope,
    PREVIEW: preview_scope,
    PYTHON
    + (
        "definition_services.create_definition:scope:clinic_id=clinic_id"
    ): publication_scope,
    PYTHON
    + (
        "definition_services.create_definition:scope:clinic_id=clinic_id#2"
    ): publication_scope,
    PYTHON
    + "definition_services._definition:scope:clinic_id=clinic_id": definition_scope,
    PYTHON + "designer._command:scope:clinic_id=clinic_id": designer_command_scope,
    PYTHON
    + "designer._authorized_designer:scope:clinic_id=clinic_id": designer_queue_scope,
    PYTHON + "run_views._command:scope:clinic_id=clinic_id": native_run_scope,
    PYTHON + "run_views._authorized_runs:scope:clinic_id=clinic_id": native_run_scope,
    PYTHON + "run_views._authorized_runs:scope:clinic_id=clinic_id#2": native_run_scope,
    PYTHON + "run_control.retry_run:scope:clinic_id=clinic_id": retry_scope,
    PYTHON
    + "receipts.validate_step_receipt:scope:clinic_id=step.clinic_id": receipt_scope,
    PYTHON
    + (
        "receipts.validate_step_receipt:scope:clinic_id=step.clinic_id#2"
    ): external_receipt_scope,
    PYTHON + "run_services.start_run:scope:clinic_id=clinic_id": run_scope,
    PYTHON + "run_services.start_run:scope:clinic_id=clinic_id#2": run_scope,
    PYTHON + "run_services.start_run:scope:clinic_id=clinic_id#3": run_scope,
    PYTHON + "run_services.cancel_run:scope:clinic_id=clinic_id": cancel_scope,
    PYTHON + "task_services.create_task:scope:clinic_id=clinic_id": task_key_scope,
    PYTHON + "queue_services.list_tasks:scope:clinic_id=clinic_id": queue_scope,
    PYTHON + "views._authorized_tasks:scope:clinic_id=clinic_id": exceptions_scope,
    PYTHON + "task_feed.task_items:scope:clinic_id=scope.clinic_id": inbox_task_scope,
    PYTHON + "today.view_today:scope:clinic_id=clinic_id": today_scope,
    PYTHON + "today.view_today:scope:clinic_id=clinic_id#2": today_scope,
    PYTHON
    + "chart_sources.read_task_sources:scope:clinic_id=clinic_id": chart_task_scope,
    PYTHON
    + "chart_sources.read_task_sources:scope:clinic_id=clinic_id#2": chart_task_scope,
    PYTHON
    + "chart_sources.read_task_sources:scope:clinic_id=clinic_id#3": chart_task_scope,
    CARE + "access.load_action:scope:clinic_id=clinic_id": care_plan_scope,
    CARE + "access.load_plan:scope:clinic_id=clinic_id": care_plan_scope,
    CARE + "plans.add_care_action:scope:clinic_id=clinic_id": care_plan_scope,
    CARE
    + "plans.create_care_plan_from_version:scope:clinic_id=clinic_id": care_plan_scope,
    CARE + "plans.flag_stale_care_plan:scope:clinic_id=clinic_id": care_plan_scope,
    CARE + "plans.mark_care_action_overdue:scope:clinic_id=clinic_id": care_plan_scope,
    CARE + "pro.assign_pro:scope:clinic_id=clinic_id": care_pro_scope,
    CARE + "pro.assign_pro:scope:clinic_id=clinic_id#2": care_pro_scope,
    CARE + "pro.assign_pro:scope:clinic_id=clinic_id#3": care_pro_scope,
    CARE + "pro.close_missed_pro_occurrence:scope:clinic_id=clinic_id": care_pro_scope,
    CARE + "pro.record_pro_response:scope:clinic_id=clinic_id": care_pro_scope,
    CARE + "recalls.materialize_recall:scope:clinic_id=clinic_id": care_recall_scope,
    CARE + "recalls.schedule_recall:scope:clinic_id=clinic_id": care_recall_scope,
    CARE + "recalls.schedule_recall:scope:clinic_id=clinic_id#2": care_recall_scope,
    CARE + "referrals._referral:scope:clinic_id=clinic_id": care_referral_scope,
    CARE
    + "referrals.link_referral_document:scope:clinic_id=clinic_id": care_referral_scope,
    CARE
    + (
        "referrals.link_referral_document:scope:clinic_id=clinic_id#2"
    ): care_referral_scope,
    PYTHON
    + "coordination_views._release_notices:scope:clinic_id=clinic_id": notice_scope,
    PYTHON + "release.current_notice:scope:clinic_id=clinic_id": notice_scope,
    PYTHON + "release.publish_release_notice:scope:clinic_id=clinic_id": notice_scope,
    PYTHON + "release.release_deliveries:scope:clinic_id=clinic_id": release_scope,
    PYTHON
    + "release.release_result_to_patient:scope:clinic_id=clinic_id": release_scope,
    PYTHON + "urgent._decide:scope:clinic_id=clinic_id": concern_scope,
    PYTHON + "urgent._latest:scope:clinic_id=clinic_id": coverage_scope,
    PYTHON + "urgent.set_urgent_coverage:scope:clinic_id=clinic_id": coverage_scope,
    PYTHON + "urgent.urgent_concerns:scope:clinic_id=clinic_id": concern_scope,
}


# --- SQL record-scope sites -----------------------------------------------------


def refused(expected: tuple[str, str], action: Callable[[], object]) -> None:
    with pytest.raises(DatabaseError) as caught, transaction.atomic():
        action()
    assert getattr(caught.value.__cause__, "sqlstate", None) == expected[0]
    assert expected[1] in str(caught.value), str(caught.value)


def raw_task(
    s: Scoped,
    clinic: UUID,
    creator: UUID,
    subject: dict[str, str],
    key: UUID | None = None,
) -> Task:
    return Task.objects.create(
        organization_id=s.graph.organization_a,
        clinic_id=clinic,
        kind="checklist",
        subject_ref=subject,
        created_by_id=creator,
        due_at=timezone.now() + timedelta(hours=1),
        idempotency_key=key or uuid4(),
        fingerprint="0" * 64,
    )


def sql_reference(kind: str) -> Callable[[Scoped], None]:
    def case(s: Scoped) -> None:
        with permission_context(s.graph, s.staff):
            refused(
                ("23514", "invalid task"),
                lambda: raw_task(s, s.a, s.staff, s.ref(s.b, kind)),
            )
            raw_task(s, s.a, s.staff, s.ref(s.a, kind))

    return case


def sql_clinic_in_tenant(s: Scoped) -> None:
    with permission_context(s.graph, s.staff):
        refused(
            ("23514", "workflow scope mismatch"),
            lambda: raw_task(
                s,
                s.graph.clinic_c,
                s.staff,
                {"kind": "clinic", "id": str(s.graph.clinic_c)},
            ),
        )
        raw_task(s, s.a, s.staff, s.ref(s.a, "clinic"))


def sql_comment(s: Scoped) -> None:
    def comment(clinic: UUID, task: str) -> object:
        from apps.workflows.models import TaskComment  # noqa: PLC0415

        return TaskComment.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=clinic,
            task_id=UUID(task),
            author_id=s.manager,
            body="Sintetico",
        )

    with permission_context(s.graph, s.manager):
        refused(
            ("42501", "comment denied"), lambda: comment(s.a, s.ref(s.b, "task")["id"])
        )
        comment(s.a, s.ref(s.a, "task")["id"])


def sql_definition_version(s: Scoped) -> None:
    from apps.workflows.models import WorkflowDefinitionVersion  # noqa: PLC0415

    def definition(version: int) -> object:
        return WorkflowDefinitionVersion.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.a,
            key="scope-only-b",
            version=version,
            steps=TIMER,
            created_by_id=s.manager,
            idempotency_key=uuid4(),
            fingerprint="0" * 64,
            creation_fingerprint="0" * 64,
        )

    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        publish_definition(
            clinic_id=s.b, key="scope-only-b", steps=TIMER, idempotency_key=uuid4()
        )
    with permission_context(s.graph, s.manager):
        refused(("23514", "definition denied"), lambda: definition(2))
        definition(1)


def _raw_run(s: Scoped, clinic: UUID, definition: UUID) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization_id=s.graph.organization_a,
        clinic_id=clinic,
        definition_version_id=definition,
        started_by_id=s.manager,
        context_refs=[s.ref(clinic, "clinic")],
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def sql_run_definition(s: Scoped) -> None:
    with permission_context(s.graph, s.manager):
        refused(("23514", "run denied"), lambda: _raw_run(s, s.a, s.definitions[s.b]))
        _raw_run(s, s.a, s.definitions[s.a])


def sql_step_run(s: Scoped) -> None:
    with permission_context(s.graph, s.manager):
        runs = {
            clinic: _raw_run(s, clinic, s.definitions[clinic]) for clinic in (s.a, s.b)
        }

        def step(run: WorkflowRun) -> object:
            return WorkflowStep.objects.create(
                organization_id=s.graph.organization_a,
                clinic_id=s.a,
                run=run,
                position=0,
            )

        refused(("42501", "step authority denied"), lambda: step(runs[s.b]))
        step(runs[s.a])


def _claimed_step(s: Scoped, *, handler: str = "timer") -> WorkflowStep:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        definitions = {
            "timer": TIMER,
            "task": [
                {
                    "handler": "task",
                    "kind": "checklist",
                    "subject": 0,
                    "owner_role": "clinic_manager",
                    "due_seconds": 3600,
                }
            ],
            "external": [{"handler": "external", "provider": "workflow-synthetic-v1"}],
        }
        definition = publish_definition(
            clinic_id=s.a,
            key="scope-claim",
            steps=definitions[handler],
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=s.a,
            definition_version_id=definition.pk,
            context_refs=[s.ref(s.a, "clinic")],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
        assert engine.claim_step(step_id=step.pk) == 1
    return step


def sql_step_operation(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration, "_dispatch", lambda _operation: None)
    external.register_adapters()
    step = _claimed_step(s, handler="external")
    operations = {}
    for clinic in (s.a, s.b):
        with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
            operations[clinic] = enqueue_operation(
                ActionOperationRequest(
                    provider="workflow-synthetic-v1",
                    clinic_id=clinic,
                    subject_type="workflows.step",
                    subject_id=step.pk,
                    idempotency_key=uuid4(),
                    payload_digest="0" * 64,
                )
            )

    def attach(operation: object) -> object:
        return WorkflowStep.objects.filter(pk=step.pk).update(
            state="waiting", operation_id=operation, wake_at=timezone.now()
        )

    with permission_context(s.graph, s.manager):
        refused(("23514", "step operation mismatch"), lambda: attach(operations[s.b]))
        assert attach(operations[s.a]) == 1


def sql_step_task(s: Scoped) -> None:
    step = _claimed_step(s, handler="task")
    with permission_context(s.graph, s.manager):
        tasks = {
            clinic: raw_task(s, clinic, s.manager, s.ref(clinic, "clinic"), key=step.pk)
            for clinic in (s.a, s.b)
        }
        for clinic, task in tasks.items():
            assign_task(
                clinic_id=clinic,
                task_id=task.pk,
                owner=TaskOwner(user_id=s.manager),
                expected_revision=1,
            )
            start_task(clinic_id=clinic, task_id=task.pk, expected_revision=2)
            complete_task(
                clinic_id=clinic,
                task_id=task.pk,
                expected_revision=3,
                evidence={"checked": True},
            )

        def attach(task: Task) -> object:
            return WorkflowStep.objects.filter(pk=step.pk).update(
                state="completed", created_task_id=task.pk
            )

        refused(("23514", "step task mismatch"), lambda: attach(tasks[s.b]))
        assert attach(tasks[s.a]) == 1


def _only_in_b(s: Scoped, role: str) -> UUID:
    user = User.objects.create(username=f"sintetico-only-b-{uuid4().hex}").pk
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=user,
            role=role,
            workflow_bundle_version=2,
        )
    return user


def _scalar(sql: str, arguments: list[UUID | str | None]) -> object:
    with connection.cursor() as cursor:
        cursor.execute(sql, arguments)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


def sql_owned(s: Scoped) -> None:
    member, _enrollment = permission_actor(s.graph, "receptionist")
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=member,
            role="nurse",
            workflow_bundle_version=2,
        )
    query = "SELECT clinic_app.workflows_owned(%s,NULL,'nurse')"
    with permission_context(s.graph, member):
        assert _scalar(query, [s.a]) is False
        assert _scalar(query, [s.b]) is True


def sql_owner_valid(s: Scoped) -> None:
    outsider = _only_in_b(s, "nurse")
    query = "SELECT clinic_app.workflows_owner_valid(%s,%s,'')"
    with permission_context(s.graph, s.manager):
        assert _scalar(query, [s.a, outsider]) is False
        assert _scalar(query, [s.b, outsider]) is True


def sql_staff_catalog(s: Scoped) -> None:
    outsider = _only_in_b(s, "nurse")
    query = (
        "SELECT count(*) FROM clinic_app.workflows_staff_catalog(%s) WHERE user_id=%s"
    )
    with permission_context(s.graph, s.manager):
        assert _scalar(query, [s.a, outsider]) == 0
        assert _scalar(query, [s.b, outsider]) == 1


def predecessor_scope(s: Scoped) -> None:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        parents = {}
        for clinic in (s.a, s.b):
            parent = create_task(
                clinic_id=clinic, spec=spec(clinic), idempotency_key=uuid4()
            )
            assign_task(
                clinic_id=clinic,
                task_id=parent.pk,
                owner=TaskOwner(user_id=s.manager),
                expected_revision=1,
            )
            start_task(clinic_id=clinic, task_id=parent.pk, expected_revision=2)
            parents[clinic] = complete_task(
                clinic_id=clinic,
                task_id=parent.pk,
                expected_revision=3,
                evidence={"checked": True},
            )
        same_clinic = create_task(
            clinic_id=s.a,
            spec=replace(spec(s.a), depends_on=parents[s.a].pk),
            idempotency_key=uuid4(),
        )
        assign_task(
            clinic_id=s.a,
            task_id=same_clinic.pk,
            owner=TaskOwner(user_id=s.manager),
            expected_revision=1,
        )
        query = "SELECT clinic_app.workflows_predecessor_ready(%s,%s)"
        assert _scalar(query, [s.a, same_clinic.pk]) is True
        assert _scalar(query, [s.b, same_clinic.pk]) is False
        assert _scalar(query, [s.a, uuid4()]) is False
        with pytest.raises(CurrentActorError):
            create_task(
                clinic_id=s.a,
                spec=replace(spec(s.a), depends_on=parents[s.b].pk),
                idempotency_key=uuid4(),
            )

        def raw_child(parent: UUID) -> object:
            return Task.objects.create(
                organization_id=s.graph.organization_a,
                clinic_id=s.a,
                created_by_id=s.manager,
                kind="checklist",
                subject_ref=s.ref(s.a, "clinic"),
                due_at=timezone.now() + timedelta(hours=1),
                depends_on_id=parent,
                idempotency_key=uuid4(),
                fingerprint="0" * 64,
            )

        refused(("23514", "invalid task"), lambda: raw_child(parents[s.b].pk))
        raw_child(parents[s.a].pk)


def care_team(s: Scoped, user: UUID, clinic: UUID, enrolled: UUID) -> None:
    with owner_context(s.graph.organization_a):
        CareTeamMembership.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=clinic,
            patient_enrollment_id=enrolled,
            user_id=user,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )


def shared_patient(s: Scoped) -> tuple[UUID, UUID, UUID]:
    """The dual-clinic clinician and one patient enrolled in both clinics.

    The same patient in both clinics leaves only the clinic comparison to
    refuse; different patients would let a patient check mask its removal.
    """
    author, enrollments = care_clinician(s)
    with owner_context(s.graph.organization_a):
        patient = PatientClinicEnrollment.objects.get(pk=enrollments[s.a]).patient_id
    there = enrollment_of(s.graph, patient, s.b)
    care_team(s, author, s.b, there)
    return author, enrollments[s.a], there


def care_sql_plan_encounter(s: Scoped) -> None:
    author, here, there = shared_patient(s)
    local = finalized_note(s, author, s.a, here)
    foreign = finalized_note(s, author, s.b, there)

    def plan(note: tuple[UUID, UUID]) -> CarePlan:
        version, encounter = note
        return CarePlan.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.a,
            enrollment_id=here,
            encounter_id=encounter,
            source_version_id=version,
            clinician_id=author,
            idempotency_key=uuid4(),
            fingerprint="0" * 64,
        )

    with permission_context(s.graph, author):
        refused(("23514", "invalid care plan"), lambda: plan(foreign))
        plan(local)
        transaction.set_rollback(True)


def care_sql_version_scope(s: Scoped) -> None:
    """The version resolver names this clinic's encounter and enrollment only."""
    author, here, there = shared_patient(s)
    local, _encounter = finalized_note(s, author, s.a, here)
    foreign, _encounter = finalized_note(s, author, s.b, there)

    def scope(version: UUID) -> list[tuple[object, ...]]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT enrollment_id FROM "
                "clinic_app.workflows_care_version_scope(%s,%s)",
                [s.a, version],
            )
            rows: list[tuple[object, ...]] = cursor.fetchall()
        return rows

    with permission_context(s.graph, author):
        assert scope(foreign) == []
        assert scope(local) == [(here,)]
        transaction.set_rollback(True)


def care_sql_occurrence_scope(s: Scoped) -> None:
    author, here, there = shared_patient(s)
    slots = {}
    with permission_context(s.graph, author):
        for clinic, enrolled in ((s.a, here), (s.b, there)):
            series = care.assign_pro(
                clinic_id=clinic,
                enrollment_id=enrolled,
                spec=pro_spec(),
                idempotency_key=uuid4(),
            )
            slots[clinic] = PROOccurrence.objects.get(assignment_id=series.pk).pk

    def scope(occurrence: UUID) -> object:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.workflows_care_occurrence_scope(%s,%s)",
                [s.a, occurrence],
            )
            row = cursor.fetchone()
        assert row is not None
        return row[0]

    with permission_context(s.graph, author):
        assert scope(slots[s.b]) is None
        assert scope(slots[s.a]) == here
        transaction.set_rollback(True)


def care_sql_referral_document(s: Scoped) -> None:
    """A documented referral stays linked to a document of its own clinic.

    Plan-less referral rows are written only by migration 0007 (trusted code,
    guard lifted), so the case writes that row shape the same way; for them
    the clinic comparison is the only check on the linked document.
    """
    author, mine, theirs = care_sides(s)
    referral = {}
    with owner_context(s.graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE clinic_app.workflows_referral DISABLE TRIGGER care_guard"
        )
        # As in migration 0007: deferred checks must not be pending at ENABLE.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        for draft in (mine.draft, theirs.draft):
            referral[draft] = uuid4()
            cursor.execute(
                "INSERT INTO clinic_app.workflows_referral (id,organization_id,"
                "clinic_id,plan_id,action_id,target_kind,target_label,status,"
                "document_id,created_by_id,revision,last_command_key,"
                "last_command_digest,created_at,updated_at) VALUES (%s,%s,%s,NULL,"
                "NULL,'unspecified',NULL,'documented',%s,%s,1,NULL,'',now(),now())",
                [referral[draft], s.graph.organization_a, s.a, draft, author],
            )
        cursor.execute(
            "ALTER TABLE clinic_app.workflows_referral ENABLE TRIGGER care_guard"
        )

    def advance(draft: UUID) -> int:
        return Referral.objects.filter(pk=referral[draft]).update(
            status="scheduled",
            revision=F("revision") + 1,
            last_command_key=uuid4(),
            last_command_digest="a" * 64,
        )

    with permission_context(s.graph, s.manager):
        refused(("23514", "referral document mismatch"), lambda: advance(theirs.draft))
        assert advance(mine.draft) == 1
        transaction.set_rollback(True)


def urgent_sql_responsible(s: Scoped) -> None:
    """A responsible role counts only in the clinic that holds it."""
    member, _enrollment = permission_actor(s.graph, "receptionist")
    _role_in_b(s, member, "nurse")

    def responsible(clinic: UUID) -> object:
        with permission_context(s.graph, member), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.workflows_urgent_responsible(%s,%s::varchar[])",
                [clinic, ["nurse"]],
            )
            row = cursor.fetchone()
            transaction.set_rollback(True)
        assert row is not None
        return row[0]

    assert responsible(s.b) is True
    assert responsible(s.a) is False


def urgent_sql_coverage_policy(s: Scoped) -> None:
    """One clinic's coverage policy never answers for another clinic's patients."""
    world = urgent_world(s.graph, coverage=False)
    session, _thread = clinic_b_portal(s.graph, world.other_enrollment)
    with as_actor(s.graph, s.manager):
        urgent.set_urgent_coverage(
            clinic_id=s.b,
            expected_version=0,
            weekdays=(0, 1, 2, 3, 4),
            opens_at=time(8, 0),
            closes_at=time(18, 0),
            response_hours=4,
            responsible_roles=("nurse",),
        )
    with runtime_role(), patient_session_context(world.session):
        assert urgent.patient_coverage_text() == (gettext(urgent.NO_COVERAGE),)
    with runtime_role(), patient_session_context(session):
        assert len(urgent.patient_coverage_text()) == 2


CARE_GUARD = "scope:workflows_care_guard():"


GUARD = "scope:workflows_guard():"
REFERENCE = "scope:workflows_reference_valid(jsonb,uuid,uuid):"
SQL_CASES: dict[str, Callable[..., None]] = {
    GUARD + "c.id=NEW.clinic_id": sql_clinic_in_tenant,
    GUARD + "t.clinic_id=NEW.clinic_id": sql_comment,
    GUARD + "d.clinic_id=NEW.clinic_id": sql_definition_version,
    GUARD + "d.clinic_id=NEW.clinic_id#2": sql_run_definition,
    GUARD + "r.clinic_id=NEW.clinic_id": sql_step_run,
    GUARD + "o.clinic_id=NEW.clinic_id": sql_step_operation,
    GUARD + "t.clinic_id=NEW.clinic_id#2": sql_step_task,
    GUARD + "parent.clinic_id=NEW.clinic_id": predecessor_scope,
    (
        "scope:workflows_predecessor_ready(uuid,uuid):child.clinic_id=clinic"
    ): predecessor_scope,
    (
        "scope:workflows_predecessor_ready(uuid,uuid):parent.clinic_id=child.clinic_id"
    ): predecessor_scope,
    "scope:workflows_owned(uuid,uuid,text):r.clinic_id=clinic": sql_owned,
    "scope:workflows_owner_valid(uuid,uuid,text):r.clinic_id=clinic": sql_owner_valid,
    REFERENCE + "identifier=clinic": sql_reference("clinic"),
    REFERENCE + "e.clinic_id=clinic": sql_reference("enrollment"),
    REFERENCE + "a.clinic_id=clinic": sql_reference("appointment"),
    REFERENCE + "t.clinic_id=clinic": sql_reference("task"),
    "scope:workflows_staff_catalog(uuid):r.clinic_id=clinic": sql_staff_catalog,
    CARE_GUARD + "d.clinic_id=NEW.clinic_id": care_sql_referral_document,
    CARE_GUARD + "e.clinic_id=NEW.clinic_id": care_sql_plan_encounter,
    (
        "scope:workflows_care_occurrence_scope(uuid,uuid):o.clinic_id=clinic"
    ): care_sql_occurrence_scope,
    (
        "scope:workflows_care_version_scope(uuid,uuid):e.clinic_id=clinic"
    ): care_sql_version_scope,
    (
        "scope:workflows_care_version_scope(uuid,uuid):n.clinic_id=e.clinic_id"
    ): care_sql_version_scope,
    (
        "scope:workflows_urgent_responsible(uuid,character varying[]):"
        "r.clinic_id=clinic"
    ): urgent_sql_responsible,
    (
        "scope:workflows_coverage_policy(uuid,uuid):v.clinic_id=clinic"
    ): urgent_sql_coverage_policy,
}


def test_every_record_scope_site_has_a_two_clinic_case() -> None:
    """Fail closed in both layers: each derived scope site has its case."""
    python = {site.sid for site in discover_sites().values() if site.kind == SCOPE}
    assert python == set(PY_CASES)
    assert set(discover_scope_conditions()) == set(SQL_CASES)


def _run(case: Callable[..., None], s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    if "monkeypatch" in inspect.signature(case).parameters:
        case(s, monkeypatch)
    else:
        case(s)


# One run per distinct case; a case may prove several sites of one service.
UNIQUE = {
    case.__qualname__ + str(getattr(case, "__closure__", "")): site
    for site, case in sorted({**PY_CASES, **SQL_CASES}.items(), reverse=True)
}


@pytest.mark.parametrize("site", sorted(UNIQUE.values()))
def test_record_scope_refuses_another_clinic_and_allows_its_own(
    scoped: Scoped, site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run({**PY_CASES, **SQL_CASES}[site], scoped, monkeypatch)


@pytest.mark.parametrize(
    "body",
    [
        "def planted(clinic_id):\n    return Unknown.lookup(clinic_id=clinic_id)\n",
        "def planted(clinic_id):\n"
        "    return Task.objects.annotate(clinic_id=clinic_id)\n",
    ],
)
def test_python_scope_derivation_fails_closed_on_unclassified_uses(body: str) -> None:
    node = ast.parse(body).body[0]
    assert isinstance(node, ast.FunctionDef)
    function = decision_sites._Function(
        "planted", access, Path(str(access.__file__)), node, access.owner_matches
    )
    with pytest.raises(AssertionError, match="unclassified"):
        decision_sites._scope_sites("planted", function)


def test_sql_scope_derivation_fails_closed_on_unclassified_uses() -> None:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE FUNCTION clinic_app.workflows_plant(clinic uuid) "
                "RETURNS boolean LANGUAGE sql AS $$ SELECT clinic IS NOT NULL $$"
            )
        with pytest.raises(AssertionError, match="unclassified record-scope use"):
            discover_scope_conditions()
        transaction.set_rollback(True)
    assert set(discover_scope_conditions()) == set(SQL_CASES)


@pytest.mark.parametrize(
    ("planted", "classified"),
    [
        (
            "CREATE FUNCTION clinic_app.workflows_plant_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "INSERT INTO clinic_app.workflows_carerefusal "
            "(id,organization_id,clinic_id) "
            "VALUES (gen_random_uuid(),NEW.organization_id,NEW.clinic_id); "
            "RETURN NEW; END $$",
            True,
        ),
        (
            "CREATE FUNCTION clinic_app.workflows_plant_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ DECLARE v clinic_app.workflows_task; BEGIN "
            "INSERT INTO clinic_app.workflows_carerefusal "
            "(id,organization_id,clinic_id) "
            "VALUES (gen_random_uuid(),v.organization_id,v.clinic_id); "
            "RETURN NEW; END $$",
            False,
        ),
    ],
)
def test_sql_scope_derivation_admits_only_a_trigger_rows_own_scope_in_values(
    planted: str, *, classified: bool
) -> None:
    # INSERT target columns are writes; VALUES may copy only NEW's own scope.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(planted)
        if classified:
            assert set(discover_scope_conditions()) == set(SQL_CASES)
        else:
            with pytest.raises(AssertionError, match="unclassified record-scope use"):
                discover_scope_conditions()
        transaction.set_rollback(True)
