"""Human adjudication applies a new demographics version and a lineage edge."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_username
from apps.identity.stepup import StepUpRequired
from apps.intake.alias_access import (
    DuplicateConflictError,
    DuplicateInputError,
    MergeCrossOrganizationError,
    review_actor,
    verified_review_session,
)
from apps.intake.demographics import (
    DEMOGRAPHICS_FIELD_VALUES,
    MAX_REASON_LENGTH,
    STATUS_FIELDS,
    _DemographicsMutation,
    _latest_demographics,
    _version_unknown,
    _version_values,
)
from apps.intake.duplicate_scope import AdjudicationScope, lock_candidate
from apps.intake.merge_execution import execute_alias_merge
from apps.intake.models import (
    DuplicateCandidate,
    MergeDecision,
    PatientAlias,
)
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
)


class DecisionKind(StrEnum):
    """The only human outcomes; deterministic signals authorize neither."""

    MERGE = "merge"
    DISMISS = "dismiss"


@dataclass(frozen=True, slots=True)
class DemographicFieldChoice:
    """A source identity and the exact demographics version the human saw."""

    patient_id: UUID
    expected_version: int


def _reason(reason: str) -> str:
    if type(reason) is not str or not reason.strip() or len(reason) > MAX_REASON_LENGTH:
        raise DuplicateInputError
    return " ".join(reason.split())


@dataclass(frozen=True, slots=True)
class _SelectedDemographics:
    versions: dict[UUID, int]
    changes: dict[str, object]
    unknown: dict[str, object]
    choices: dict[str, dict[str, str | int]]


def _select_demographics(
    scope: AdjudicationScope, field_choices: Mapping[str, DemographicFieldChoice]
) -> _SelectedDemographics:
    enrollments = (scope.original, scope.survivor)
    current = {
        row.patient_id: _latest_demographics(
            scope.clinic.organization_id, row.patient_id
        )
        for row in enrollments
    }
    versions = {
        patient_id: row.version if row else 0 for patient_id, row in current.items()
    }
    changes: dict[str, object] = {}
    unknown: dict[str, object] = {}
    choices: dict[str, dict[str, str | int]] = {}
    patients = {row.patient_id: row.patient for row in enrollments}
    for field, choice in field_choices.items():
        if (
            field not in DEMOGRAPHICS_FIELD_VALUES
            or type(choice) is not DemographicFieldChoice
            or type(choice.patient_id) is not UUID
            or type(choice.expected_version) is not int
            or choice.expected_version < 0
        ):
            raise DuplicateInputError
        if choice.patient_id not in current:
            raise MergeCrossOrganizationError
        if choice.expected_version != versions[choice.patient_id]:
            code = "demographics_stale"
            raise DuplicateConflictError(code)
        changes[field] = _version_values(
            current[choice.patient_id], patients[choice.patient_id]
        )[field]
        if field in STATUS_FIELDS:
            unknown[field] = _version_unknown(current[choice.patient_id]).get(field, "")
        choices[field] = {
            "patient_id": str(choice.patient_id),
            "expected_version": choice.expected_version,
        }
    return _SelectedDemographics(versions, changes, unknown, choices)


def adjudicate_duplicate(  # noqa: PLR0913 - exact scoped adjudication contract
    *,
    clinic_id: UUID,
    candidate_id: UUID,
    decision: str,
    survivor_patient_id: UUID,
    field_choices: Mapping[str, DemographicFieldChoice],
    reason: str,
) -> MergeDecision:
    """Lock and adjudicate exactly one candidate, never repoint its old records."""
    reason = _reason(reason)
    try:
        kind = DecisionKind(decision)
    except (TypeError, ValueError) as error:
        raise DuplicateInputError from error
    if not isinstance(field_choices, Mapping):
        raise DuplicateInputError
    if kind is DecisionKind.DISMISS and field_choices:
        raise DuplicateInputError
    with transaction.atomic():
        scope = lock_candidate(
            clinic_id=clinic_id,
            candidate_id=candidate_id,
            survivor_patient_id=survivor_patient_id,
        )
        fields = _select_demographics(scope, field_choices)
        proof = verified_review_session(scope.actor_id)
        verified_at = proof.verified_at
        if verified_at is None:
            raise StepUpRequired
        receipt = MergeDecision.objects.create(
            organization_id=scope.clinic.organization_id,
            clinic_id=clinic_id,
            candidate=scope.candidate,
            decision=kind.value,
            adjudicator_id=scope.actor_id,
            verified_session_id=proof.pk,
            step_up_verified_at=verified_at,
            merged_patient_id=scope.original.patient_id,
            survivor_patient_id=scope.survivor.patient_id,
            merged_version=fields.versions[scope.original.patient_id],
            survivor_version=fields.versions[scope.survivor.patient_id],
            result_version=fields.versions[scope.survivor.patient_id] + 1
            if kind is DecisionKind.MERGE
            else None,
            reason=reason,
            field_choices=fields.choices,
        )
        match kind:
            case DecisionKind.MERGE:
                receipt.issued_invitations = execute_alias_merge(
                    receipt,
                    _DemographicsMutation(
                        clinic=scope.clinic,
                        enrollment=scope.survivor,
                        actor_id=scope.actor_id,
                        actor_label=current_actor_username(),
                        expected_version=fields.versions[scope.survivor.patient_id],
                        changes=fields.changes,
                        unknown=fields.unknown,
                        reason=reason,
                        source="staff_recorded",
                    ),
                    scope.clinic_ids,
                )
                scope.candidate.state = DuplicateCandidate.State.MERGED
                event = "intake.duplicate.merged"
            case DecisionKind.DISMISS:
                scope.candidate.state = DuplicateCandidate.State.DISMISSED
                event = "intake.duplicate.dismissed"
            case unreachable:
                assert_never(unreachable)
        scope.candidate.save(update_fields=("state",))
        record_phase1_event(event, clinic_id=clinic_id, affected_record_id=receipt.pk)
        return receipt


def unmerge(*, clinic_id: UUID, merge_decision_id: UUID, reason: str) -> PatientAlias:
    """Deactivate one edge; all later survivor-bound records remain there."""
    reason = _reason(reason)
    with transaction.atomic():
        actor_id, _clinic = review_actor(clinic_id)
        proof = verified_review_session(actor_id)
        acquire_advisory_locks((clinic_lock_key(clinic_id),))
        alias = (
            PatientAlias.objects.select_for_update(of=("self",))
            .filter(
                merge_decision_id=merge_decision_id,
                merge_decision__clinic_id=clinic_id,
            )
            .first()
        )
        if alias is None:
            raise MergeCrossOrganizationError
        if not alias.active:
            return alias
        alias.active = False
        alias.deactivated_by_id = actor_id
        alias.deactivation_session_id = proof.pk
        alias.deactivated_at = timezone.now()
        alias.deactivation_reason = reason
        alias.save(
            update_fields=(
                "active",
                "deactivated_by",
                "deactivation_session",
                "deactivated_at",
                "deactivation_reason",
            )
        )
        record_phase1_event(
            "intake.duplicate.unmerged",
            clinic_id=clinic_id,
            affected_record_id=alias.pk,
        )
        return alias
