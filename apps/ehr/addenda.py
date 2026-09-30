"""Addendum drafts: other clinicians' contributions to an encounter (plan item 27).

The main SOAP draft has a single author (the encounter's assigned physician)
and an author lock. Any other clinician holding ``clinical.write`` for the
encounter's patient in the encounter's clinic writes an addendum instead: one
draft per author and encounter, stored separately, so it can never overwrite
the main draft. Addendum autosave has the same contract as the main draft's:
compare-and-set on ``expected_revision``, an idempotency receipt bound to the
request digest, and a line comparison instead of last-write-wins. It has no
lock, because only its author can write it.

Every refusal (unknown or foreign encounter/addendum, missing permission,
another author's addendum) is the same ``ClinicalAccessDeniedError`` and
writes nothing. RLS (``has_permission`` + author) and ``ehr_addendum_guard``
(same organization, clinic, patient and open encounter; never the assigned
physician) re-decide every write. Finalizing, releasing and showing addenda
to readers belongs to later todos (28 timeline, 29 templates).
"""

from __future__ import annotations

from hashlib import sha256
from typing import Final
from uuid import UUID

import rfc8785
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.audit.services import record_phase1_event
from apps.ehr import autosave
from apps.ehr.autosave import (
    AutosaveIdempotencyError,
    AutosaveResult,
    AutosaveStatus,
    text_diff,
)
from apps.ehr.episodes import _enrollment_id, _require
from apps.ehr.models import AddendumSaveReceipt, Encounter, EncounterAddendum
from apps.ehr.services import (
    MAX_CONTENT,
    ClinicalAccessDeniedError,
    ClinicalConflictError,
)

INVALID_ADDENDUM: Final = "Addendum autosave request is invalid."


def open_addendum(*, clinic_id: UUID, encounter_id: UUID) -> EncounterAddendum:
    """Open (or resume) the caller's addendum draft on one encounter."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    actor = _require(
        "clinical.write", clinic_id, _enrollment_id(clinic_id, encounter.patient_id)
    )
    if actor == encounter.physician_id:
        msg = "use_main_draft"
        raise ClinicalConflictError(msg)
    if encounter.state != Encounter.State.OPEN:
        msg = "encounter_closed"
        raise ClinicalConflictError(msg)
    drafts = EncounterAddendum.objects.filter(
        encounter=encounter, author_id=actor, state=EncounterAddendum.State.DRAFT
    )
    with transaction.atomic():
        existing = drafts.first()
        if existing is not None:
            return existing
        try:
            with transaction.atomic():
                addendum = EncounterAddendum.objects.create(
                    organization_id=encounter.organization_id,
                    clinic_id=clinic_id,
                    encounter=encounter,
                    patient_id=encounter.patient_id,
                    author_id=actor,
                )
        except IntegrityError:
            winner = drafts.first()
            if winner is None:
                raise
            return winner
        record_phase1_event(
            "ehr.addendum.opened", clinic_id=clinic_id, affected_record_id=addendum.pk
        )
        return addendum


def _validated(expected_revision: object, command_id: object, text: object) -> str:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
        or not isinstance(command_id, UUID)
        or not isinstance(text, str)
        or len(text) > MAX_CONTENT
    ):
        raise ValidationError(INVALID_ADDENDUM)
    return text


def _digest(addendum_id: UUID, expected_revision: int, text: str) -> str:
    canonical = rfc8785.dumps(
        {
            "addendum_id": str(addendum_id),
            "expected_revision": expected_revision,
            "text": text,
        }
    )
    return sha256(canonical).hexdigest()


def _authorized(clinic_id: UUID, addendum_id: UUID) -> EncounterAddendum:
    """Return the caller's own addendum in this clinic under current permission."""
    addendum = EncounterAddendum.objects.filter(
        pk=addendum_id, clinic_id=clinic_id
    ).first()
    if addendum is None:
        raise ClinicalAccessDeniedError
    actor = _require(
        "clinical.write", clinic_id, _enrollment_id(clinic_id, addendum.patient_id)
    )
    if addendum.author_id != actor:
        raise ClinicalAccessDeniedError
    return addendum


def autosave_addendum(
    *,
    clinic_id: UUID,
    addendum_id: UUID,
    expected_revision: int,
    editor_command_id: UUID,
    text: str,
) -> AutosaveResult:
    """Save one addendum revision; same CAS and idempotency as the main draft."""
    text = _validated(expected_revision, editor_command_id, text)
    _authorized(clinic_id, addendum_id)
    digest = _digest(addendum_id, expected_revision, text)
    now = autosave.utc_now()
    with transaction.atomic():
        locked = (
            EncounterAddendum.objects.select_for_update().filter(pk=addendum_id).first()
        )
        if locked is None or locked.state != EncounterAddendum.State.DRAFT:
            msg = "precondition_failed"
            raise ClinicalConflictError(msg)
        receipt = AddendumSaveReceipt.objects.filter(
            addendum=locked, command_id=editor_command_id
        ).first()
        if receipt is not None:
            if receipt.request_sha256 != digest:
                raise AutosaveIdempotencyError
            return AutosaveResult(
                status=AutosaveStatus.REPLAYED,
                revision=receipt.revision,
                saved_at=receipt.saved_at,
            )
        # A new command needs an open encounter even when the text is
        # unchanged and no addendum row is written (round-2 B2); the receipt
        # guard re-decides the same binding in the database.
        if (
            Encounter.objects.filter(pk=locked.encounter_id)
            .values_list("state", flat=True)
            .first()
            != Encounter.State.OPEN
        ):
            msg = "encounter_closed"
            raise ClinicalConflictError(msg)
        stored = locked.text or ""
        if locked.revision != expected_revision:
            return AutosaveResult(
                status=AutosaveStatus.CONFLICT,
                revision=locked.revision,
                diff=(text_diff("addendum", stored, text),),
            )
        # An unchanged text is acknowledged without a new revision.
        if text != stored:
            locked.text = text
            locked.text_sha256 = sha256(text.encode()).hexdigest()
            locked.revision += 1
            locked.save(update_fields=("text", "text_sha256", "revision", "updated_at"))
            record_phase1_event(
                "ehr.addendum.saved", clinic_id=clinic_id, affected_record_id=locked.pk
            )
        AddendumSaveReceipt.objects.create(
            organization_id=locked.organization_id,
            addendum=locked,
            command_id=editor_command_id,
            request_sha256=digest,
            base_revision=expected_revision,
            revision=locked.revision,
            saved_at=now,
        )
        return AutosaveResult(
            status=AutosaveStatus.SAVED, revision=locked.revision, saved_at=now
        )


def view_addendum(*, clinic_id: UUID, addendum_id: UUID) -> EncounterAddendum:
    """Read the caller's own addendum draft (its author is its only reader)."""
    return _authorized(clinic_id, addendum_id)
