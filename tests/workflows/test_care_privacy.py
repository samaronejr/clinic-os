"""Todo 52 B6: care clinical content is read and written by the patient's clinicians.

RP matrix: managers and organization admins have no clinical read by default and
staff routing authority never records a patient-reported answer. Routing
metadata (plans, administrative actions, tasks) stays visible to staff.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from apps.tenancy.envelope import issue_tenant_key
from apps.workflows.care import services as care
from apps.workflows.care.models import (
    CareAction,
    CarePlan,
    PROAssignment,
    PROOccurrence,
    Referral,
)
from apps.workflows.care.validation import CareActionSpec, ReferralSpec, return_window
from apps.workflows.models import Task
from apps.workflows.validation import (
    TaskOwner,
    WorkflowConflictError,
    WorkflowInputError,
)
from django.utils import timezone

from ehr.test_addenda import clinician
from identity.permission_support import owner_context
from workflows.permission_support import permission_actor
from workflows.test_careplans import (
    acting,
    instruction,
    plan_of,
    pro_terms,
    return_in,
    rows,
    world,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
CALM = {f"item_{n}": "0" for n in range(1, 10)}


def care_clinician(graph: RbacGraph, role: str, enrollment: UUID) -> UUID:
    """A registered care-team member of ``role`` on the v2 workflow bundle."""
    user = clinician(graph, role, enrollment)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(user_id=user).update(workflow_bundle_version=2)
    return user


def referral_spec() -> CareActionSpec:
    start, end = return_window(now=timezone.now(), weeks=2)
    return CareActionSpec(
        kind="referral",
        window_start=start,
        window_end=end,
        referral=ReferralSpec(target_kind="specialty", target_label="Psiquiatria"),
    )


def seeded(graph: RbacGraph) -> tuple[UUID, CarePlan, PROOccurrence]:
    """A plan with an instruction, a referral and a return window, and a PRO slot."""
    chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6), instruction(), referral_spec())
    with acting(graph, version.author_id):
        assignment = care.assign_pro(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            spec=pro_terms(occurrences=1),
            idempotency_key=uuid4(),
        )
    slot = rows(graph, PROOccurrence, assignment_id=assignment.pk)[0]
    return chart.enrollment, plan, slot


def seen(graph: RbacGraph, actor: UUID) -> dict[str, set[object]]:
    with acting(graph, actor):
        return {
            "plans": set(CarePlan.objects.values_list("pk", flat=True)),
            "kinds": set(CareAction.objects.values_list("kind", flat=True)),
            "referrals": set(Referral.objects.values_list("pk", flat=True)),
            "pro": set(PROAssignment.objects.values_list("pk", flat=True)),
            "slots": set(PROOccurrence.objects.values_list("pk", flat=True)),
        }


@pytest.mark.parametrize("role", ["clinic_manager", "org_admin"])
def test_routing_roles_see_no_clinical_care_content(
    rbac_graph: RbacGraph, role: str
) -> None:
    graph = rbac_graph
    _enrollment, plan, _slot = seeded(graph)
    staff, _own = permission_actor(graph, role)
    view = seen(graph, staff)
    # Plans and administrative/referral actions are routing metadata.
    assert view["plans"] == {plan.pk}
    assert view["kinds"] == {"return_window", "referral"}
    # The instruction text, the referral destination and PRO data are clinical.
    assert view["referrals"] == set()
    assert view["pro"] == set()
    assert view["slots"] == set()


def test_care_team_physician_reads_all_and_nurse_reads_observations_only(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    enrollment, plan, slot = seeded(graph)
    physician = care_clinician(graph, "physician", enrollment)
    nurse = care_clinician(graph, "nurse", enrollment)
    outsider, _own = permission_actor(graph, "physician")
    assert seen(graph, physician)["kinds"] == {
        "return_window",
        "referral",
        "patient_instruction",
    }
    assert len(seen(graph, physician)["referrals"]) == 1
    assert seen(graph, physician)["slots"] == {slot.pk}
    nursing = seen(graph, nurse)
    assert nursing["slots"] == {slot.pk}
    assert "patient_instruction" not in nursing["kinds"]
    assert nursing["referrals"] == set()
    # Another clinic physician without care of this patient sees none of it.
    assert seen(graph, outsider) == {
        "plans": set(),
        "kinds": set(),
        "referrals": set(),
        "pro": set(),
        "slots": set(),
    }
    assert plan.pk in seen(graph, physician)["plans"]


def test_pro_response_is_recorded_by_the_patients_nurse_never_a_manager(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    enrollment, _plan, slot = seeded(graph)
    boss, _own = permission_actor(graph, "clinic_manager")
    with acting(graph, boss), pytest.raises(CurrentActorError):
        care.record_pro_response(
            clinic_id=graph.clinic_a,
            occurrence_id=slot.pk,
            answers=CALM,
            expected_revision=slot.revision,
        )
    assert rows(graph, PROOccurrence, pk=slot.pk)[0].state == "pending"
    nurse = care_clinician(graph, "nurse", enrollment)
    with acting(graph, nurse):
        recorded = care.record_pro_response(
            clinic_id=graph.clinic_a,
            occurrence_id=slot.pk,
            answers={**CALM, "item_9": "2"},
            expected_revision=slot.revision,
        )
    assert recorded.state == "responded"
    task = rows(graph, Task, pk=recorded.response_task_id)[0]
    # A source-backed system task in the response role's queue; concern = urgent.
    assert (task.created_by_id, task.owner_user_id, task.owner_role) == (
        None,
        None,
        "physician",
    )
    assert (task.kind, task.state, task.priority) == ("review", "assigned", "urgent")
    assert task.subject_ref == {"kind": "enrollment", "id": str(enrollment)}
    # The response role's clinician on the patient's care team reads the answer.
    reviewer = care_clinician(graph, "physician", enrollment)
    with acting(graph, reviewer):
        stored = PROOccurrence.objects.get(pk=slot.pk)
        assert stored.answers is not None
        assert stored.answers["answers"]["item_9"] == "2"
        assert Task.objects.filter(pk=task.pk).exists()


def test_response_queue_must_be_a_role_that_can_read_the_response(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    chart, version = world(graph)
    with acting(graph, version.author_id):
        for role in ("receptionist", "clinic_manager", "finance"):
            with pytest.raises(WorkflowInputError):
                care.assign_pro(
                    clinic_id=graph.clinic_a,
                    enrollment_id=chart.enrollment,
                    spec=pro_terms(response_role=role),
                    idempotency_key=uuid4(),
                )
    assert not rows(graph, PROAssignment)


def test_referral_coordinator_reads_and_advances_only_its_referral(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _enrollment, plan, _slot = seeded(graph)
    action = rows(graph, CareAction, plan_id=plan.pk, kind="referral")[0]
    boss, _own = permission_actor(graph, "clinic_manager")
    coordinator, _own = permission_actor(graph, "receptionist")
    bystander, _own = permission_actor(graph, "receptionist")
    with acting(graph, boss):
        care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            owner=TaskOwner(user_id=coordinator),
            expected_revision=action.revision,
        )
    referral = rows(graph, Referral, action_id=action.pk)[0]
    assert seen(graph, bystander)["referrals"] == set()
    with acting(graph, bystander), pytest.raises(CurrentActorError):
        care.advance_referral(
            clinic_id=graph.clinic_a,
            referral_id=referral.pk,
            status="scheduled",
            expected_revision=referral.revision,
        )
    with acting(graph, coordinator):
        visible = Referral.objects.get(pk=referral.pk)
        assert visible.target_label == "Psiquiatria"
        advanced = care.advance_referral(
            clinic_id=graph.clinic_a,
            referral_id=referral.pk,
            status="scheduled",
            expected_revision=referral.revision,
        )
    assert advanced.status == "scheduled"
    # The destination is clinical content: the coordinating owner's manager
    # still sees only the routing task, never the referral itself.
    assert seen(graph, boss)["referrals"] == set()


def test_pro_answer_digest_is_tenant_keyed_and_replays_across_rotation(
    rbac_graph: RbacGraph,
) -> None:
    # Review N1: a plain hash of PHQ-9 answers falls to the 4^9 answer space.
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
        key=lambda slot: slot.sequence,
    )

    def respond(slot: PROOccurrence, answers: dict[str, str]) -> PROOccurrence:
        with acting(graph, version.author_id):
            return care.record_pro_response(
                clinic_id=graph.clinic_a,
                occurrence_id=slot.pk,
                answers=answers,
                expected_revision=slot.revision,
            )

    respond(first, CALM)
    with owner_context(graph.organization_a):
        issue_tenant_key()
    respond(second, CALM)
    (bound_first,) = rows(graph, PROOccurrence, pk=first.pk)
    (bound_second,) = rows(graph, PROOccurrence, pk=second.pk)
    # Same actor and answers: a plain hash would repeat; the newer key differs.
    assert bound_first.last_command_digest != bound_second.last_command_digest
    # A retry bound under the older key still replays; other answers conflict.
    assert respond(first, CALM).revision == bound_first.revision
    with pytest.raises(WorkflowConflictError):
        respond(first, {**CALM, "item_1": "1"})


def test_instruction_fingerprint_is_tenant_keyed_and_replays_across_rotation(
    rbac_graph: RbacGraph,
) -> None:
    # Instruction text and referral targets are dictionary-recoverable when hashed.
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))
    spec = instruction()
    key = uuid4()

    def add(request_key: UUID, terms: CareActionSpec) -> CareAction:
        with acting(graph, version.author_id):
            return care.add_care_action(
                clinic_id=graph.clinic_a,
                plan_id=plan.pk,
                spec=terms,
                idempotency_key=request_key,
            )

    first = add(key, spec)
    with owner_context(graph.organization_a):
        issue_tenant_key()
    assert add(uuid4(), spec).fingerprint != first.fingerprint
    assert add(key, spec).pk == first.pk
    with pytest.raises(WorkflowConflictError):
        add(key, replace(spec, instruction="Retornar em jejum."))
