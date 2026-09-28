"""Episodes: a clinician-named grouping of one patient's encounters (plan item 27).

Authority follows the RP matrix's clinical narrative row through
``has_permission``: ``clinical.write`` (reads: ``clinical.read``) for the
episode's clinic and the patient's enrollment, which requires a current
professional registration and care-team or open-encounter scope. Linking an
encounter also requires its assigned physician (``_encounter_actor``), the same
patient and clinic, and an open episode. The database re-decides every input:
RLS through ``has_permission`` and ``ehr_assigned``, the binding trigger for
clinic, patient and state. Unknown, foreign-clinic and other-patient records
share one denial.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.ehr.models import Encounter, Episode, EpisodeEncounter
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _encounter_actor,
)
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.overlay_content import validate_overlay_text
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from uuid import UUID

MAX_EPISODE_TITLE: Final = 160
INVALID_TITLE: Final = "Informe um título de episódio em texto simples."


@dataclass(frozen=True, slots=True)
class EncounterEpisodes:
    """The encounter's episode link and the patient's open episodes."""

    linked: Episode | None
    open_episodes: tuple[Episode, ...]


def _enrollment_id(clinic_id: UUID, patient_id: UUID) -> UUID | None:
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=patient_id
    ).first()
    return enrollment.pk if enrollment is not None else None


def _require(permission: str, clinic_id: UUID, enrollment_id: UUID | None) -> UUID:
    if enrollment_id is None:
        raise ClinicalAccessDeniedError
    try:
        return require_permission(
            permission, clinic_id=clinic_id, patient_enrollment_id=enrollment_id
        )
    except CurrentActorError as error:
        raise ClinicalAccessDeniedError from error


def open_episode(*, clinic_id: UUID, enrollment_id: UUID, title: str) -> Episode:
    """Open a titled episode for one enrolled patient."""
    actor = _require("clinical.write", clinic_id, enrollment_id)
    title = title.strip() if isinstance(title, str) else ""
    if not title or len(title) > MAX_EPISODE_TITLE:
        raise ValidationError(INVALID_TITLE)
    validate_overlay_text(title)
    enrollment = PatientClinicEnrollment.objects.get(pk=enrollment_id)
    with transaction.atomic():
        episode = Episode.objects.create(
            organization_id=enrollment.organization_id,
            clinic_id=clinic_id,
            patient_id=enrollment.patient_id,
            title=title,
            opened_by_id=actor,
        )
        record_phase1_event(
            "ehr.episode.opened", clinic_id=clinic_id, affected_record_id=episode.pk
        )
        return episode


def close_episode(*, clinic_id: UUID, episode_id: UUID) -> Episode:
    """Close an episode; a retry returns the closed row unchanged."""
    episode = Episode.objects.filter(pk=episode_id, clinic_id=clinic_id).first()
    if episode is None:
        raise ClinicalAccessDeniedError
    actor = _require(
        "clinical.write", clinic_id, _enrollment_id(clinic_id, episode.patient_id)
    )
    with transaction.atomic():
        # The update policy admits only open rows, so a closed one is absent.
        locked = Episode.objects.select_for_update().filter(pk=episode.pk).first()
        if locked is None:
            return Episode.objects.get(pk=episode.pk)
        locked.state = Episode.State.CLOSED
        locked.closed_at = timezone.now()
        locked.closed_by_id = actor
        locked.save(update_fields=("state", "closed_at", "closed_by"))
        record_phase1_event(
            "ehr.episode.closed", clinic_id=clinic_id, affected_record_id=locked.pk
        )
        return locked


def link_encounter(
    *, clinic_id: UUID, encounter_id: UUID, episode_id: UUID
) -> EpisodeEncounter:
    """Group an encounter under an open episode of the same patient and clinic."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    actor = _encounter_actor(clinic_id, encounter)
    episode = Episode.objects.filter(pk=episode_id, clinic_id=clinic_id).first()
    if episode is None or episode.patient_id != encounter.patient_id:
        raise ClinicalAccessDeniedError
    _require(
        "clinical.write", clinic_id, _enrollment_id(clinic_id, encounter.patient_id)
    )
    with transaction.atomic():
        if Episode.objects.select_for_update().filter(pk=episode.pk).first() is None:
            msg = "episode_closed"
            raise ClinicalConflictError(msg)
        existing = EpisodeEncounter.objects.filter(encounter=encounter).first()
        if existing is not None:
            if existing.episode_id == episode.pk:
                return existing
            msg = "already_linked"
            raise ClinicalConflictError(msg)
        link = EpisodeEncounter.objects.create(
            organization_id=encounter.organization_id,
            episode=episode,
            encounter=encounter,
            linked_by_id=actor,
        )
        record_phase1_event(
            "ehr.episode.linked", clinic_id=clinic_id, affected_record_id=link.pk
        )
        return link


def encounter_episodes(*, clinic_id: UUID, encounter_id: UUID) -> EncounterEpisodes:
    """Read the encounter's link and the patient's open episodes for its author."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    _encounter_actor(clinic_id, encounter)
    _require(
        "clinical.read", clinic_id, _enrollment_id(clinic_id, encounter.patient_id)
    )
    link = (
        EpisodeEncounter.objects.filter(encounter=encounter)
        .select_related("episode")
        .first()
    )
    return EncounterEpisodes(
        linked=link.episode if link is not None else None,
        open_episodes=tuple(
            Episode.objects.filter(
                clinic_id=clinic_id,
                patient_id=encounter.patient_id,
                state=Episode.State.OPEN,
            ).order_by("opened_at", "pk")
        ),
    )
