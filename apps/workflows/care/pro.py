"""Patient-reported outcomes: scheduled slots and a human-owned response queue.

A missing response is recorded as ``no_response`` ('sem resposta') and nothing
else: it is never read as wellness, refusal or any clinical meaning. A response
always lands in a human queue; concern only raises that task's priority.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from uuid import UUID, uuid5

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.ehr.calculators.contracts import Refusal
from apps.intake.models import PatientClinicEnrollment
from apps.workflows import engine
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care import pro_instruments
from apps.workflows.care.access import (
    bind_command,
    keyed_digests,
    occurrence_enrollment,
    require_clinician,
    require_observer,
    require_staff,
    save_command,
)
from apps.workflows.care.models import CarePlan, PROAssignment, PROOccurrence
from apps.workflows.care.validation import PROSpec, validate_pro
from apps.workflows.care.vocab import NO_RESPONSE_GRACE_DAYS
from apps.workflows.validation import (
    WorkflowConflictError,
    WorkflowInputError,
    digest,
)


@transaction.atomic
def assign_pro(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    spec: PROSpec,
    idempotency_key: UUID,
) -> PROAssignment:
    """Assign an available instrument on a bounded schedule; clinician only."""
    if not isinstance(idempotency_key, UUID) or not isinstance(enrollment_id, UUID):
        raise WorkflowInputError
    terms = validate_pro(spec)
    clinic = require_staff(clinic_id=clinic_id, permission="tasks.view")
    actor = require_clinician(clinic_id=clinic_id, enrollment_id=enrollment_id)
    if not PatientClinicEnrollment.objects.filter(
        pk=enrollment_id, clinic_id=clinic_id
    ).exists():
        raise WorkflowAccessDeniedError
    if (
        spec.plan_id is not None
        and not CarePlan.objects.filter(
            pk=spec.plan_id, clinic_id=clinic_id, enrollment_id=enrollment_id
        ).exists()
    ):
        raise WorkflowAccessDeniedError
    fingerprints = keyed_digests(
        {"actor": str(actor), "enrollment": str(enrollment_id), **terms}
    )
    replay = PROAssignment.objects.filter(
        clinic_id=clinic_id, idempotency_key=idempotency_key
    ).first()
    if replay is not None:
        if replay.fingerprint not in fingerprints:
            raise WorkflowConflictError
        return replay
    # A slot due before its series exists could never be answered: the scanner
    # would record it as 'no_response' at once. Replays above stay idempotent.
    if spec.first_due_at < engine.utc_now():
        raise WorkflowInputError
    assignment = PROAssignment.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        plan_id=spec.plan_id,
        instrument_key=spec.instrument_key,
        instrument_version=pro_instruments.version(spec.instrument_key),
        first_due_at=spec.first_due_at,
        interval_days=spec.interval_days,
        occurrences=spec.occurrences,
        response_role=spec.response_role,
        staffing_policy_ref=spec.staffing_policy_ref,
        created_by_id=actor,
        idempotency_key=idempotency_key,
        fingerprint=fingerprints[-1],
    )
    for sequence in range(1, spec.occurrences + 1):
        PROOccurrence.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            assignment=assignment,
            sequence=sequence,
            due_at=spec.first_due_at
            + timedelta(days=(sequence - 1) * spec.interval_days),
            created_by_id=actor,
        )
    record_phase1_event(
        "workflows.pro.assigned", clinic_id=clinic_id, affected_record_id=assignment.pk
    )
    return assignment


def _complete_if_settled(assignment: PROAssignment) -> None:
    """Complete the assignment once no slot is pending."""
    if PROOccurrence.objects.filter(assignment=assignment, state="pending").exists():
        return
    assignment.last_command_key = uuid5(assignment.pk, "complete")
    assignment.last_command_digest = digest({"assignment": str(assignment.pk)})
    assignment.state = "completed"
    save_command(assignment, ("state",), "workflows.pro.completed")


@transaction.atomic
def record_pro_response(
    *,
    clinic_id: UUID,
    occurrence_id: UUID,
    answers: Mapping[str, str],
    expected_revision: int,
) -> PROOccurrence:
    """Record a response; the database queues it for the response role's review.

    The recorder is the patient's nurse (observations) or clinician: a
    patient-reported answer is clinical data, never staff routing work.
    """
    require_staff(clinic_id=clinic_id, permission="tasks.view")
    enrollment = occurrence_enrollment(clinic_id=clinic_id, occurrence_id=occurrence_id)
    require_observer(clinic_id=clinic_id, enrollment_id=enrollment)
    occurrence = (
        PROOccurrence.objects.select_for_update()
        .filter(pk=occurrence_id, clinic_id=clinic_id)
        .first()
    )
    assignment = (
        PROAssignment.objects.select_for_update()
        .filter(pk=occurrence.assignment_id)
        .first()
        if occurrence is not None
        else None
    )
    if occurrence is None or assignment is None:
        raise WorkflowAccessDeniedError
    key = assignment.instrument_key
    if not isinstance(answers, Mapping) or not all(
        isinstance(item, str) and isinstance(value, str)
        for item, value in answers.items()
    ):
        raise WorkflowInputError
    calculation = pro_instruments.COMPUTE[key](answers)
    if isinstance(calculation, Refusal):
        raise WorkflowInputError
    if not bind_command(occurrence, expected_revision, "respond", dict(answers)):
        return occurrence
    if occurrence.state != "pending":
        raise WorkflowConflictError
    occurrence.state = "responded"
    occurrence.answers = {
        "instrument": key,
        "answers": dict(answers),
        "score": calculation.value,
    }
    # Concern only raises the priority of the review task the guard emits.
    occurrence.concerning = pro_instruments.concerning(key, answers)
    occurrence.response_task_id = uuid5(occurrence.pk, "pro-review")
    occurrence.recorded_at = engine.utc_now()
    save_command(
        occurrence,
        ("state", "answers", "concerning", "response_task_id", "recorded_at"),
        "workflows.pro_occurrence.responded",
    )
    record_phase1_event(
        "workflows.task.created",
        clinic_id=clinic_id,
        affected_record_id=occurrence.response_task_id,
    )
    _complete_if_settled(assignment)
    return occurrence


@transaction.atomic
def close_missed_pro_occurrence(*, clinic_id: UUID, occurrence_id: UUID) -> bool:
    """Record one slot past its grace period as 'no_response' and nothing more.

    Returns False when the slot is no longer due (answered, closed or early).
    """
    require_staff(clinic_id=clinic_id, permission="tasks.assign")
    occurrence = (
        PROOccurrence.objects.select_for_update()
        .filter(pk=occurrence_id, clinic_id=clinic_id)
        .first()
    )
    if occurrence is None:
        raise WorkflowAccessDeniedError
    cutoff = engine.utc_now() - timedelta(days=NO_RESPONSE_GRACE_DAYS)
    if occurrence.state != "pending" or occurrence.due_at > cutoff:
        return False
    occurrence.last_command_key = uuid5(occurrence.pk, "no_response")
    occurrence.last_command_digest = digest({"occurrence": str(occurrence.pk)})
    occurrence.state = "no_response"
    save_command(occurrence, ("state",), "workflows.pro_occurrence.no_response")
    assignment = (
        PROAssignment.objects.select_for_update()
        .filter(pk=occurrence.assignment_id, state="active")
        .first()
    )
    if assignment is not None:
        _complete_if_settled(assignment)
    return True
