"""Committed care records for the authority census, authored by the guard actor.

Every row is created through the real services, so the guards under test see
exactly the data production code would leave behind: finalized note versions
(one per scenario, each on its own patient), a plan with open, assigned, overdue
and task-less actions, a referral with an unlinked referral document, a plan
flagged after an amendment, a plan that is stale but not yet flagged, a due
recall and a past-due PRO series (both seeded under the injected database clock).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.ehr.services import create_draft, open_encounter
from apps.identity.models import CareTeamMembership
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.prescription.models import DocumentKind
from apps.prescription.services import create_document_draft
from apps.scheduling.availability_creation import create_availability
from apps.workflows.care import plans
from apps.workflows.care import services as care
from apps.workflows.care.models import CareAction, PROOccurrence, Referral
from apps.workflows.care.validation import (
    CareActionSpec,
    PROSpec,
    ReferralSpec,
    return_window,
)
from apps.workflows.validation import TaskOwner
from django.utils import timezone

from ehr.longitudinal_support import chart_world, finalized
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_context
from scheduling.appointment_service_support import (
    AppointmentSetup,
    arrived_synthetic_appointment,
)
from workflows.clock_support import database_clock
from workflows.test_careplans import amended

if TYPE_CHECKING:
    from datetime import datetime

    from workflows.guard_cases import GuardWorld


@dataclass(frozen=True)
class CareWorld:
    """Primary keys only: cases reload rows so revisions are always current."""

    new_version: UUID
    plan: UUID
    window: UUID
    colleague: UUID
    assigned: UUID
    overdue: UUID
    overdue_task: UUID
    note: UUID
    referral: UUID
    referral_draft: UUID
    flagged: UUID
    stale_plan: UUID
    pro: UUID
    occurrence: UUID
    response_task: UUID
    recall: UUID


@dataclass(frozen=True)
class _Chart:
    version: ClinicalDocumentVersion
    encounter: UUID
    enrollment: UUID


def _chart(world: GuardWorld, template: UUID, *, scheduled: bool = False) -> _Chart:
    """A finalized note on a new patient the actor is on the care team of.

    A scheduled chart sits on an arrived appointment of the actor: referral
    documents are created only from such an encounter.
    """
    graph, actor = world.graph, world.actor
    with owner_context(graph.organization_a):
        patient = Patient.objects.create(
            organization_id=graph.organization_a,
            full_name="Sintetico Care Census",
            birth_date=date(1990, 1, 1),
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        )
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient_enrollment=enrollment,
            user_id=actor,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
        if not scheduled:
            encounter = Encounter.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_id=patient.pk,
                physician_id=actor,
                unscheduled_reason="documentation_only",
            )
    if scheduled:
        with as_actor(graph, graph.shared_user):
            create_availability(
                clinic_id=graph.clinic_a,
                practitioner_id=actor,
                start_local="2035-06-04T08:00",
                end_local="2035-06-04T12:00",
                idempotency_key=uuid4(),
            )
            appointment = arrived_synthetic_appointment(
                AppointmentSetup(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    actor_id=graph.shared_user,
                    practitioner_id=actor,
                    enrollment_id=enrollment.pk,
                    patient_id=patient.pk,
                ),
                start_local="2035-06-04T09:00",
                end_local="2035-06-04T10:00",
            )
        with as_actor(graph, actor):
            encounter = open_encounter(
                clinic_id=graph.clinic_a, appointment_id=appointment.pk
            )
    with as_actor(graph, actor):
        draft = create_draft(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, template_id=template
        )
    return _Chart(finalized(graph, draft), encounter.pk, enrollment.pk)


def _window(weeks: int) -> CareActionSpec:
    start, end = return_window(now=timezone.now(), weeks=weeks)
    return CareActionSpec(kind="return_window", window_start=start, window_end=end)


def _end(action: CareAction) -> datetime:
    return action.window_end


def seed_care_world(world: GuardWorld) -> CareWorld:
    graph, actor, clinic = world.graph, world.actor, world.clinic
    template = chart_world(graph).version.template_id
    new, first, second, third = (
        _chart(world, template, scheduled=index == 1) for index in range(4)
    )
    now = timezone.now()
    past = CareActionSpec(
        kind="staff_coordination",
        window_start=now - timedelta(days=3),
        window_end=now - timedelta(days=1),
    )
    note = CareActionSpec(
        kind="patient_instruction",
        window_start=now,
        window_end=now + timedelta(days=3),
        instruction="Manter hidratacao e repouso relativo.",
    )
    refer = CareActionSpec(
        kind="referral",
        window_start=now,
        window_end=now + timedelta(days=14),
        referral=ReferralSpec(target_kind="specialty", target_label="Cardiologia"),
    )
    with as_actor(graph, actor):
        draft = create_document_draft(
            clinic_id=clinic, encounter_id=first.encounter, kind=DocumentKind.REFERRAL
        )
    with permission_context(graph, actor):
        plan = care.create_care_plan_from_version(
            clinic_id=clinic,
            version_id=first.version.pk,
            actions=(_window(6), _window(8), past, note, refer),
            idempotency_key=uuid4(),
        )
        flagged_plan = care.create_care_plan_from_version(
            clinic_id=clinic,
            version_id=second.version.pk,
            actions=(_window(6),),
            idempotency_key=uuid4(),
        )
    amended(graph, second.version)
    with permission_context(graph, actor):
        assert plans.flag_stale_care_plan(clinic_id=clinic, plan_id=flagged_plan.pk)
        stale_plan = care.create_care_plan_from_version(
            clinic_id=clinic,
            version_id=third.version.pk,
            actions=(_window(6),),
            idempotency_key=uuid4(),
        )
    amended(graph, third.version)
    with permission_context(graph, actor):
        by_end = sorted(CareAction.objects.filter(plan_id=plan.pk), key=_end)
        window, assigned = (a for a in by_end if a.kind == "return_window")
        assigned = care.assign_care_action(
            clinic_id=clinic,
            action_id=assigned.pk,
            owner=TaskOwner(user_id=actor),
            expected_revision=assigned.revision,
        )
        overdue = next(a for a in by_end if a.kind == "staff_coordination")
        assert overdue.task_id is not None
        instruction = next(a for a in by_end if a.kind == "patient_instruction")
        referral = Referral.objects.get(plan_id=plan.pk)
        flagged = CareAction.objects.get(plan_id=flagged_plan.pk)
        assert flagged.review_state == "required"
    # A staff action another manager created: its task is not the actor's.
    with permission_context(graph, world.other):
        colleague = care.add_care_action(
            clinic_id=clinic,
            plan_id=plan.pk,
            spec=_window(7),
            idempotency_key=uuid4(),
        )
    # A past-due PRO series: assigned while the database clock is a month back.
    with (
        database_clock(graph.organization_a, lambda: now - timedelta(days=31)),
        permission_context(graph, actor),
    ):
        pro = care.assign_pro(
            clinic_id=clinic,
            enrollment_id=first.enrollment,
            spec=PROSpec(
                instrument_key="phq9",
                first_due_at=now - timedelta(days=30),
                interval_days=7,
                occurrences=2,
                response_role="physician",
                staffing_policy_ref="eg3-pending-staffing-policy",
            ),
            idempotency_key=uuid4(),
        )
        occurrence = PROOccurrence.objects.get(assignment_id=pro.pk, sequence=1)
    # The second slot is answered: its review task is the PRO system task.
    with permission_context(graph, actor):
        answered = PROOccurrence.objects.get(assignment_id=pro.pk, sequence=2)
        response_task = care.record_pro_response(
            clinic_id=clinic,
            occurrence_id=answered.pk,
            answers={f"item_{n}": "0" for n in range(1, 10)},
            expected_revision=answered.revision,
        ).response_task_id
    assert response_task is not None
    # A due recall: scheduled one day out while the database clock is ten days back.
    with (
        database_clock(graph.organization_a, lambda: now - timedelta(days=10)),
        permission_context(graph, actor),
    ):
        recall = care.schedule_recall(
            clinic_id=clinic,
            enrollment_id=first.enrollment,
            reason="routine_return",
            interval_days=1,
            owner_role="receptionist",
            idempotency_key=uuid4(),
        )
    return CareWorld(
        new_version=new.version.pk,
        plan=plan.pk,
        window=window.pk,
        colleague=colleague.pk,
        assigned=assigned.pk,
        overdue=overdue.pk,
        overdue_task=overdue.task_id,
        note=instruction.pk,
        referral=referral.pk,
        referral_draft=draft.pk,
        flagged=flagged.pk,
        stale_plan=stale_plan.pk,
        pro=pro.pk,
        occurrence=occurrence.pk,
        response_task=response_task,
        recall=recall.pk,
    )
