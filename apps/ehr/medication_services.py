"""Reported medication statements and explicit, revision-bound reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.longitudinal_models import MedicationStatement
from apps.ehr.longitudinal_values import ORIGINS, ProvenanceTag, decimal_string
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError, decided
from apps.identity.overlay_content import validate_overlay_text

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

MAX_MEDICATION_TEXT: Final = 240
MAX_RECONCILIATION: Final = 100
DOSE_UNITS: Final = ("mg", "g", "ug", "mL", "[IU]", "{tablet}", "{drop}")
ROUTES: Final = (
    "oral",
    "inhaled",
    "topical",
    "subcutaneous",
    "intramuscular",
    "intravenous",
    "other",
)
INVALID_MEDICATION: Final = _(
    "Check the medication, dose, route, frequency and origin."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class MedicationInput:
    """Structured reported use; not an order, catalog match or prescription."""

    text: str
    dose_value: str
    dose_unit: str
    route: str
    times_per_day: str
    provenance: str
    status: str = "unknown"
    effective_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MedicationReconciliation:
    """One clinician-reviewed status and the exact revision reviewed."""

    statement_id: UUID
    expected_revision: int
    status: str


def record_medication_statement(
    *, clinic_id: UUID, enrollment_id: UUID, medication: MedicationInput
) -> MedicationStatement:
    """Preserve the reported origin until a separate reconciliation."""
    authority = partial(
        patient_scope, clinic_id=clinic_id, enrollment_id=enrollment_id, action="write"
    )
    scope = authority()
    dose = decimal_string(medication.dose_value)
    frequency = decimal_string(medication.times_per_day)
    text = medication.text.strip()
    if (
        not text
        or len(text) > MAX_MEDICATION_TEXT
        or Decimal(dose) <= 0
        or Decimal(frequency) <= 0
        or medication.dose_unit not in DOSE_UNITS
        or medication.route not in ROUTES
        or medication.provenance not in ORIGINS
        or medication.status not in MedicationStatement.Status.values
        or (
            medication.effective_at is not None
            and timezone.is_naive(medication.effective_at)
        )
    ):
        raise ValidationError(INVALID_MEDICATION)
    validate_overlay_text(text)
    with decided(authority):
        row = MedicationStatement.objects.create(
            organization_id=scope.organization_id,
            clinic_id=clinic_id,
            patient_id=scope.patient_id,
            performer_id=scope.user_id,
            origin=medication.provenance,
            provenance=medication.provenance,
            effective_at=medication.effective_at or timezone.now(),
            status=medication.status,
            details={
                "text": text,
                "dose": {"value": dose, "unit": medication.dose_unit},
                "route": medication.route,
                "frequency": {"times_per_day": frequency},
            },
        )
        record_phase1_event(
            "ehr.medication.recorded", clinic_id=clinic_id, affected_record_id=row.pk
        )
        return row


def reconcile_medications(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    statements: tuple[MedicationReconciliation, ...],
) -> tuple[MedicationStatement, ...]:
    """Review exactly the selected revisions; a stale row rolls back the whole set."""
    authority = partial(
        patient_scope, clinic_id=clinic_id, enrollment_id=enrollment_id, action="write"
    )
    scope = authority()
    commands = {command.statement_id: command for command in statements}
    if (
        not statements
        or len(commands) != len(statements)
        or len(statements) > MAX_RECONCILIATION
        or any(
            command.status not in MedicationStatement.Status.values
            for command in statements
        )
    ):
        raise ValidationError(INVALID_MEDICATION)
    with decided(authority):
        rows = list(
            MedicationStatement.objects.select_for_update()
            .filter(clinic_id=clinic_id, patient_id=scope.patient_id, pk__in=commands)
            .order_by("pk")
        )
        if len(rows) != len(commands):
            raise ClinicalAccessDeniedError
        if any(row.revision != commands[row.pk].expected_revision for row in rows):
            reason = "stale_revision"
            raise ClinicalConflictError(reason)
        for row in rows:
            row.status = commands[row.pk].status
            row.provenance = ProvenanceTag.CONFIRMED
            row.reconciled_by_id = scope.user_id
            row.reconciled_at = timezone.now()
            row.revision += 1
            row.save(
                update_fields=(
                    "status",
                    "provenance",
                    "reconciled_by",
                    "reconciled_at",
                    "revision",
                )
            )
            record_phase1_event(
                "ehr.medication.reconciled",
                clinic_id=clinic_id,
                affected_record_id=row.pk,
            )
        return tuple(rows)
