"""Physician-authored, append-only history; never inferred absence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.ehr.models import Allergy, Encounter, HistoryAssessment, Problem
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError, _denied
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole

if TYPE_CHECKING:
    from uuid import UUID


MAX_REASON = 255
MAX_DESCRIPTION = 1000


@dataclass(frozen=True)
class HistoryChange:
    """Bounded user input for one category revision."""

    kind: str
    expected_revision: int
    state: str
    description: str
    status: str
    reason: str
    entry_id: UUID | None = None


@dataclass(frozen=True)
class HistoryContext:
    """Current category plus exact retained versions and provenance."""

    kind: str
    label: str
    state: str
    state_label: str
    revision: int
    entries: list[Problem | Allergy]
    versions: list[Problem | Allergy]
    assessments: list[HistoryAssessment]
    can_write: bool


def authorize_history(
    clinic_id: UUID, encounter_id: UUID, *, write: bool = False
) -> Encounter:
    """Recheck canonical role and care relationship on every request, including POST."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    try:
        require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    except CurrentActorError:
        _denied(clinic_id, encounter_id, "role_denied")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ehr_assigned(%s), clinic_app.ehr_history_care(%s)",
            [encounter_id, encounter_id],
        )
        assigned, care = cursor.fetchone() or (False, False)
    if not care or (write and not assigned):
        _denied(
            clinic_id, encounter_id, "not_assigned" if write else "no_care_relationship"
        )
    return encounter


def _model(kind: str) -> type[Problem | Allergy]:
    if kind not in HistoryAssessment.Kind.values:
        message = "Categoria inválida."
        raise ValidationError(message)
    return Problem if kind == "problem" else Allergy


def _context(encounter: Encounter, kind: str) -> HistoryContext:
    model = _model(kind)
    assessments = list(
        HistoryAssessment.objects.filter(
            clinic_id=encounter.clinic_id, patient_id=encounter.patient_id, kind=kind
        ).order_by("-revision")
    )
    versions: list[Problem | Allergy] = list(
        model.objects.filter(
            assessment__clinic_id=encounter.clinic_id,
            assessment__patient_id=encounter.patient_id,
        )
        .select_related("assessment")
        .order_by("-assessment__revision")
    )
    current: dict[UUID, Problem | Allergy] = {}
    for version in versions:
        current.setdefault(version.entry_id, version)
    latest = assessments[0] if assessments else None
    state = latest.state if latest else "not_assessed"
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_assigned(%s)", [encounter.pk])
        can_write = bool(cursor.fetchone()[0])
    return HistoryContext(
        kind,
        str(HistoryAssessment.Kind(kind).label),
        state,
        str(HistoryAssessment.State(state).label),
        latest.revision if latest else 0,
        list(current.values()),
        versions,
        assessments,
        can_write,
    )


def read_history(*, clinic_id: UUID, encounter_id: UUID, kind: str) -> HistoryContext:
    """Audit clinical reads before materializing current and historical values."""
    encounter = authorize_history(clinic_id, encounter_id)
    record_phase1_event(
        "ehr.history.viewed", clinic_id=clinic_id, affected_record_id=encounter_id
    )
    return _context(encounter, kind)


def save_history(
    *, clinic_id: UUID, encounter_id: UUID, change: HistoryChange
) -> HistoryAssessment:
    """Append one explicit declaration or entry version with atomic audit evidence."""
    encounter = authorize_history(clinic_id, encounter_id, write=True)
    kind, expected_revision, state = change.kind, change.expected_revision, change.state
    status, entry_id = change.status, change.entry_id
    model = _model(kind)
    reason, description = change.reason.strip(), change.description.strip()
    if (
        state not in HistoryAssessment.State.values
        or not reason
        or len(reason) > MAX_REASON
        or len(description) > MAX_DESCRIPTION
        or expected_revision < 0
        or (
            state == "documented"
            and (not description or status not in Problem.Status.values)
        )
        or (state != "documented" and (description or status or entry_id))
    ):
        message = (
            "Informe o estado explicitamente e preencha os campos do registro "
            "e o motivo."
        )
        raise ValidationError(message)
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.PHYSICIAN,)
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"ehr-history:{clinic_id}:{encounter.patient_id}:{kind}"],
        )
        context = _context(encounter, kind)
        if context.revision != expected_revision:
            message = "stale_revision"
            raise ClinicalConflictError(message)
        if state != "documented" and context.entries:
            message = "documented_entries"
            raise ClinicalConflictError(message)
        previous = next(
            (entry for entry in context.entries if entry.entry_id == entry_id), None
        )
        if entry_id is not None and previous is None:
            raise ClinicalAccessDeniedError
        if previous is not None:
            authorize_history(clinic_id, previous.assessment.encounter_id, write=True)
        assessment = HistoryAssessment.objects.create(
            organization_id=encounter.organization_id,
            clinic_id=clinic_id,
            patient_id=encounter.patient_id,
            encounter=encounter,
            kind=kind,
            state=state,
            revision=expected_revision + 1,
            author_id=actor,
            reason=reason,
        )
        # The binding trigger stamps provenance without exposing identity rows.
        assessment.refresh_from_db(fields=("author_label", "created_at"))
        if state == "documented":
            model.objects.create(
                organization_id=encounter.organization_id,
                assessment=assessment,
                entry_id=entry_id or uuid4(),
                version=previous.version + 1 if previous else 1,
                description=description,
                status=status,
            )
        record_phase1_event(
            "ehr.history.saved", clinic_id=clinic_id, affected_record_id=assessment.pk
        )
        return assessment
