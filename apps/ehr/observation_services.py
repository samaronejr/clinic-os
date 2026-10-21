"""Append observed facts; confirmation is a separate, explicit clinician action."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.ehr.longitudinal_access import PatientScope, patient_scope
from apps.ehr.longitudinal_models import Observation
from apps.ehr.longitudinal_values import (
    CODES,
    ORIGINS,
    ProvenanceTag,
    bmi,
    decimal_string,
    observation_value,
)
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError, decided

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

INVALID_OBSERVATION = _("Check the observation, origin and measurement time.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationInput:
    """One local code-set-v1 measurement, never an inferred normal finding."""

    kind: str
    value: str
    unit: str
    provenance: str
    effective_at: datetime | None = None
    reference_low: str | None = None
    reference_high: str | None = None


def _value(spec: ObservationInput) -> dict[str, str | None | list[str]]:
    value = observation_value(kind=spec.kind, value=spec.value, unit=spec.unit)
    if spec.provenance not in ORIGINS or (
        spec.effective_at is not None and timezone.is_naive(spec.effective_at)
    ):
        raise ValidationError(INVALID_OBSERVATION)
    low = decimal_string(spec.reference_low) if spec.reference_low is not None else None
    high = (
        decimal_string(spec.reference_high) if spec.reference_high is not None else None
    )
    if (CODES[spec.kind].choices and (low is not None or high is not None)) or (
        low is not None and high is not None and Decimal(low) > Decimal(high)
    ):
        raise ValidationError(INVALID_OBSERVATION)
    return {"value": value, "reference_low": low, "reference_high": high}


def _insert(
    *, scope: PatientScope, spec: ObservationInput, derived_from: tuple[UUID, ...] = ()
) -> Observation:
    payload = _value(spec)
    payload["derived_from"] = [str(identifier) for identifier in derived_from]
    row = Observation.objects.create(
        organization_id=scope.organization_id,
        clinic_id=scope.clinic_id,
        patient_id=scope.patient_id,
        performer_id=scope.user_id,
        origin=spec.provenance,
        provenance=spec.provenance,
        effective_at=spec.effective_at or timezone.now(),
        kind=spec.kind,
        unit=spec.unit,
        value=payload,
    )
    record_phase1_event(
        "ehr.observation.recorded", clinic_id=scope.clinic_id, affected_record_id=row.pk
    )
    return row


def record_observation(
    *, clinic_id: UUID, enrollment_id: UUID, observation: ObservationInput
) -> Observation:
    """Record a numeric or coded value without confirming its origin."""
    authority = partial(
        patient_scope,
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        action="observe",
    )
    scope = authority()
    _value(observation)
    if observation.kind == "bmi":
        raise ValidationError(_("Record weight and height together to calculate BMI."))
    with decided(authority):
        return _insert(scope=scope, spec=observation)


def record_vitals(
    *, clinic_id: UUID, enrollment_id: UUID, measurements: tuple[ObservationInput, ...]
) -> tuple[Observation, ...]:
    """Commit a measurement set atomically; BMI derives only from this set."""
    authority = partial(
        patient_scope,
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        action="observe",
    )
    scope = authority()
    by_kind = {spec.kind: spec for spec in measurements}
    if (
        not measurements
        or len(by_kind) != len(measurements)
        or any(spec.kind in {"bmi", "pain_present"} for spec in measurements)
        or len({spec.provenance for spec in measurements}) != 1
        or len({spec.effective_at for spec in measurements}) != 1
    ):
        raise ValidationError(INVALID_OBSERVATION)
    for spec in measurements:
        _value(spec)
    calculated = None
    if "weight" in by_kind and "height" in by_kind:
        calculated = bmi(weight=by_kind["weight"].value, height=by_kind["height"].value)
    measured_at = measurements[0].effective_at or timezone.now()
    with decided(authority):
        rows = [
            _insert(scope=scope, spec=replace(spec, effective_at=measured_at))
            for spec in measurements
        ]
        if calculated is not None:
            sources = tuple(row.pk for row in rows if row.kind in {"weight", "height"})
            rows.append(
                _insert(
                    scope=scope,
                    spec=ObservationInput(
                        kind="bmi",
                        value=calculated,
                        unit="kg/m2",
                        provenance=measurements[0].provenance,
                        effective_at=measured_at,
                    ),
                    derived_from=sources,
                )
            )
        return tuple(rows)


def confirm_observation(
    *, clinic_id: UUID, observation_id: UUID, expected_revision: int
) -> Observation:
    """Explicit review advances only toward confirmed, retaining the origin."""

    def authority() -> Observation:
        row = Observation.objects.filter(pk=observation_id, clinic_id=clinic_id).first()
        if row is None:
            raise ClinicalAccessDeniedError
        from apps.intake.models import PatientClinicEnrollment  # noqa: PLC0415

        enrollment = PatientClinicEnrollment.objects.filter(
            clinic_id=clinic_id, patient_id=row.patient_id
        ).first()
        if enrollment is None:
            raise ClinicalAccessDeniedError
        patient_scope(clinic_id=clinic_id, enrollment_id=enrollment.pk, action="write")
        return row

    authority()
    with decided(authority):
        row = Observation.objects.select_for_update().filter(pk=observation_id).first()
        if row is None:
            raise ClinicalAccessDeniedError
        if row.revision != expected_revision:
            reason = "stale_revision"
            raise ClinicalConflictError(reason)
        if row.provenance == ProvenanceTag.CONFIRMED:
            return row
        from apps.identity.current_context import current_actor_id  # noqa: PLC0415

        row.provenance = ProvenanceTag.CONFIRMED
        row.confirmed_by_id = current_actor_id()
        row.confirmed_at = timezone.now()
        row.revision += 1
        row.save(
            update_fields=("provenance", "confirmed_by", "confirmed_at", "revision")
        )
        record_phase1_event(
            "ehr.observation.confirmed", clinic_id=clinic_id, affected_record_id=row.pk
        )
        return row
