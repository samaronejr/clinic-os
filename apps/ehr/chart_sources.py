"""Read-only chart excerpts through current clinical permissions and source RLS."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.core.chart_sources import (
    CHART_LIMIT,
    ChartBatch,
    ChartSection,
    ChartSource,
    CoverageState,
)
from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.models import (
    Allergy,
    ClinicalDocumentVersion,
    Encounter,
    HistoryAssessment,
    MedicationStatement,
    Observation,
    Problem,
)
from apps.ehr.services import ClinicalAccessDeniedError, view_version
from apps.identity.current_context import (
    require_current_actor_clinic_roles,
    require_permission,
)
from apps.identity.models import UserClinicRole
from apps.intake.alias_access import patient_alias_set
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from uuid import UUID


def chart_enrollment(
    *,
    clinic_id: UUID,
    enrollment_id: UUID | None = None,
    encounter_id: UUID | None = None,
) -> UUID:
    """Resolve one POST selector only after canonical clinician admission."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    if (enrollment_id is None) == (encounter_id is None):
        raise ClinicalAccessDeniedError
    if encounter_id is not None:
        encounter = Encounter.objects.filter(
            pk=encounter_id, clinic_id=clinic_id
        ).first()
        if encounter is None:
            raise ClinicalAccessDeniedError
        enrollment_id = (
            PatientClinicEnrollment.objects.filter(
                clinic_id=clinic_id, patient_id=encounter.patient_id
            )
            .values_list("pk", flat=True)
            .first()
        )
    if enrollment_id is None:
        raise ClinicalAccessDeniedError
    require_permission(
        "clinical.read", clinic_id=clinic_id, patient_enrollment_id=enrollment_id
    )
    patient_scope(clinic_id=clinic_id, enrollment_id=enrollment_id, action="read")
    return enrollment_id


def _history(*, clinic_id: UUID, patients: tuple[UUID, ...]) -> ChartBatch:
    sources: list[ChartSource] = []
    coverage: list[tuple[ChartSection, CoverageState]] = []
    for kind, model, section in (
        ("problem", Problem, "active_problems"),
        ("allergy", Allergy, "allergies"),
    ):
        section_name: ChartSection = (
            "active_problems" if section == "active_problems" else "allergies"
        )
        rows = tuple(
            model.objects.filter(
                assessment__clinic_id=clinic_id,
                assessment__patient_id__in=patients,
            )
            .select_related("assessment")
            .order_by("-assessment__created_at", "-version", "pk")[: CHART_LIMIT + 1]
        )
        seen: set[UUID] = set()
        for row in rows[:CHART_LIMIT]:
            if row.entry_id in seen:
                continue
            seen.add(row.entry_id)
            if row.status != "active":
                continue
            sources.append(
                ChartSource(
                    section_name,
                    kind,
                    row.pk,
                    row.version,
                    row.assessment.created_at,
                    row.description,
                )
            )
        latest = (
            HistoryAssessment.objects.filter(
                clinic_id=clinic_id, patient_id__in=patients, kind=kind
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if latest is not None and latest.state == "none_documented":
            sources.append(
                ChartSource(
                    section_name,
                    f"{kind}_assessment",
                    latest.pk,
                    latest.revision,
                    latest.created_at,
                    latest.state,
                    "documented_none",
                )
            )
        coverage.append(
            (section_name, "limited" if len(rows) > CHART_LIMIT else "complete")
        )
    return ChartBatch(tuple(sources), tuple(coverage))


def read_chart(*, clinic_id: UUID, enrollment_id: UUID) -> ChartBatch:
    """Materialize only this patient's currently permitted, source-bound facts."""
    chart_enrollment(clinic_id=clinic_id, enrollment_id=enrollment_id)
    scope = patient_scope(
        clinic_id=clinic_id, enrollment_id=enrollment_id, action="read"
    )
    aliases = patient_alias_set(clinic_id=clinic_id, patient_id=scope.patient_id)
    history = _history(clinic_id=clinic_id, patients=aliases.patient_ids)
    sources = list(history.sources)
    coverage = list(history.coverage)
    medications = tuple(
        MedicationStatement.objects.filter(
            clinic_id=clinic_id, patient_id__in=aliases.patient_ids, status="active"
        ).order_by("-effective_at", "pk")[: CHART_LIMIT + 1]
    )
    observations = tuple(
        Observation.objects.filter(
            clinic_id=clinic_id, patient_id__in=aliases.patient_ids
        ).order_by("-effective_at", "pk")[: CHART_LIMIT + 1]
    )
    encounters = tuple(
        Encounter.objects.filter(
            clinic_id=clinic_id, patient_id__in=aliases.patient_ids
        ).order_by("-created_at", "pk")[: CHART_LIMIT + 1]
    )
    sources.extend(
        ChartSource(
            "medications",
            "medication",
            medication.pk,
            medication.revision,
            medication.effective_at,
            json.dumps(
                {
                    "details": medication.details,
                    "status": medication.status,
                    "provenance": medication.provenance,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        for medication in medications[:CHART_LIMIT]
    )
    sources.extend(
        ChartSource(
            "observations",
            "observation",
            observation.pk,
            observation.revision,
            observation.effective_at,
            json.dumps(
                {
                    "kind": observation.kind,
                    "value": observation.value,
                    "unit": observation.unit,
                    "provenance": observation.provenance,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        for observation in observations[:CHART_LIMIT]
    )
    sources.extend(
        ChartSource(
            "chronology",
            "encounter",
            encounter.pk,
            encounter.revision,
            encounter.created_at,
            encounter.get_state_display(),
        )
        for encounter in encounters[:CHART_LIMIT]
    )
    # The version SELECT policy includes the current restricted-note grant.
    # Do not query RestrictedNote or count refused notes beside this projection.
    notes = tuple(
        ClinicalDocumentVersion.objects.filter(
            document__encounter__clinic_id=clinic_id,
            document__encounter__patient_id__in=aliases.patient_ids,
            state="finalized",
        )
        .only("pk")
        .order_by("-finalized_at", "pk")[: CHART_LIMIT + 1]
    )
    for note in notes[:CHART_LIMIT]:
        version = view_version(clinic_id=clinic_id, version_id=note.pk)
        if version.finalized_at is None:
            raise ClinicalAccessDeniedError
        sources.append(
            ChartSource(
                "prior_decisions",
                "note",
                version.pk,
                version.version,
                version.finalized_at,
                json.dumps(
                    {"assessment": version.assessment, "plan": version.plan},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
    for section_name, records in (
        ("medications", medications),
        ("observations", observations),
        ("chronology", encounters),
        ("prior_decisions", notes),
    ):
        section: ChartSection
        match section_name:
            case "medications":
                section = "medications"
            case "observations":
                section = "observations"
            case "chronology":
                section = "chronology"
            case _:
                section = "prior_decisions"
        coverage.append(
            (section, "limited" if len(records) > CHART_LIMIT else "complete")
        )
    record_phase1_event(
        "ehr.timeline.viewed", clinic_id=clinic_id, affected_record_id=enrollment_id
    )
    return ChartBatch(tuple(sources), tuple(coverage))
