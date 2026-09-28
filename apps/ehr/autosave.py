"""Durable server autosave of SOAP drafts (plan item 27, ADR-004, SD-8a).

Every autosave is one idempotent command. It is authorized exactly like an
explicit save: the version must be the current actor's own draft on an
encounter they are assigned to (``view_version`` plus ``_encounter_actor``).
The write goes through the unchanged ``record_clinical_note`` (H-2), whose
compare-and-set on ``expected_revision`` is the only way a revision moves.

- ``editor_command_id`` is the idempotency key. Its receipt binds the key to
  the exact request digest: a replay returns the stored acknowledgement and
  writes nothing, and a reused key with other content is refused.
- A stale ``expected_revision`` never overwrites: the result is a conflict
  carrying the current revision and a per-section comparison, and the caller
  must merge explicitly (last-write-wins does not exist).
- One editor session (browser tab) holds the draft lock. Each autosave renews
  it for two minutes; another session is refused as ``locked_by_other`` and
  may request a handover, which the holder's next autosave grants.
- ``section_edit_epochs`` counts acknowledged edits per section (todo 42).

Nothing here is stored in the browser, and no clinical text reaches audit,
logs or receipts: receipts hold revisions and a SHA-256 request digest.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Final
from uuid import UUID

import rfc8785
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.ehr.models import ClinicalDocumentVersion, DraftEditState, DraftSaveReceipt
from apps.ehr.services import (
    MAX_CONTENT,
    SOAP_FIELDS,
    ClinicalConflictError,
    _denied,
    _encounter_actor,
    record_clinical_note,
    view_version,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

LOCK_TTL: Final = timedelta(minutes=2)
INVALID_AUTOSAVE: Final = "Autosave request is invalid."


def utc_now() -> datetime:
    """Clock seam for lock expiry and acknowledgement times."""
    return timezone.now()


class AutosaveStatus(StrEnum):
    """Machine-consumed outcome of one autosave command."""

    SAVED = "saved"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    LOCKED_BY_OTHER = "locked_by_other"
    HANDOVER_REQUESTED = "handover_requested"


class AutosaveIdempotencyError(Exception):
    """An ``editor_command_id`` was reused for a different request."""


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One line of a section comparison: ``same``, ``added`` or ``removed``."""

    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class SectionDiff:
    """Saved text versus the editor's text for one changed SOAP section."""

    section: str
    theirs: str
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class AutosaveResult:
    """The acknowledged or current revision and, on conflict, the comparison."""

    status: AutosaveStatus
    revision: int
    saved_at: datetime | None = None
    diff: tuple[SectionDiff, ...] = ()
    handed_over: bool = False


def _validated(
    expected_revision: object,
    editor_command_id: object,
    editor_session_id: object,
    sections: object,
) -> dict[str, str]:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
        or not isinstance(editor_command_id, UUID)
        or not isinstance(editor_session_id, UUID)
        or not isinstance(sections, dict)
        or set(sections) != set(SOAP_FIELDS)
        or any(
            not isinstance(value, str) or len(value) > MAX_CONTENT
            for value in sections.values()
        )
    ):
        raise ValidationError(INVALID_AUTOSAVE)
    return {field: sections[field] for field in SOAP_FIELDS}


def request_digest(
    *, version_id: UUID, expected_revision: int, sections: Mapping[str, str]
) -> str:
    """Bind an idempotency key to the exact request it acknowledged."""
    canonical = rfc8785.dumps(
        {
            "expected_revision": expected_revision,
            "sections": dict(sections),
            "version_id": str(version_id),
        }
    )
    return sha256(canonical).hexdigest()


def section_diff(
    stored: Mapping[str, str], mine: Mapping[str, str]
) -> tuple[SectionDiff, ...]:
    """Compare line by line; ``removed`` lines exist only in the saved text."""
    result: list[SectionDiff] = []
    for field in SOAP_FIELDS:
        theirs, ours = stored.get(field, ""), mine[field]
        if theirs == ours:
            continue
        before, after = theirs.splitlines(), ours.splitlines()
        lines: list[DiffLine] = []
        matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                lines.extend(DiffLine("same", text) for text in before[i1:i2])
                continue
            lines.extend(DiffLine("removed", text) for text in before[i1:i2])
            lines.extend(DiffLine("added", text) for text in after[j1:j2])
        result.append(SectionDiff(field, theirs, tuple(lines)))
    return tuple(result)


def _edit_state(version: ClinicalDocumentVersion) -> DraftEditState:
    state = DraftEditState.objects.select_for_update().filter(version=version).first()
    if state is not None:
        return state
    return DraftEditState.objects.create(
        organization_id=version.organization_id,
        version=version,
        author_id=version.author_id,
        section_edit_epochs=dict.fromkeys(SOAP_FIELDS, 0),
    )


def _hold(state: DraftEditState, session: UUID, now: datetime) -> None:
    if state.lock_holder != session:
        # A pending request targeted the previous holder, not this one.
        state.handover_requested_by = None
    state.lock_holder = session
    state.lock_expires_at = now + LOCK_TTL
    if state.handover_requested_by == session:
        state.handover_requested_by = None


def _save_state(state: DraftEditState) -> None:
    state.save(
        update_fields=(
            "section_edit_epochs",
            "lock_holder",
            "lock_expires_at",
            "handover_requested_by",
            "updated_at",
        )
    )


def _replay(
    version: ClinicalDocumentVersion, command_id: UUID, digest: str
) -> AutosaveResult | None:
    receipt = DraftSaveReceipt.objects.filter(
        version=version, command_id=command_id
    ).first()
    if receipt is None:
        return None
    if receipt.request_sha256 != digest:
        raise AutosaveIdempotencyError
    return AutosaveResult(
        status=AutosaveStatus.REPLAYED,
        revision=receipt.revision,
        saved_at=receipt.saved_at,
    )


def _lock_refusal(
    state: DraftEditState,
    version: ClinicalDocumentVersion,
    session: UUID,
    now: datetime,
    *,
    request_handover: bool,
) -> AutosaveResult | None:
    """Refuse while another session holds a live lock; record a handover ask."""
    if (
        state.lock_holder in (None, session)
        or state.lock_expires_at is None
        or state.lock_expires_at <= now
    ):
        return None
    if not request_handover:
        return AutosaveResult(
            status=AutosaveStatus.LOCKED_BY_OTHER, revision=version.revision
        )
    if state.handover_requested_by != session:
        state.handover_requested_by = session
        _save_state(state)
    return AutosaveResult(
        status=AutosaveStatus.HANDOVER_REQUESTED, revision=version.revision
    )


def _write(  # noqa: PLR0913 - one locked save and its bookkeeping
    clinic_id: UUID,
    version: ClinicalDocumentVersion,
    state: DraftEditState,
    content: dict[str, str],
    command: tuple[UUID, UUID, int, str],
    now: datetime,
) -> AutosaveResult:
    command_id, session, expected_revision, digest = command
    handover_to = (
        state.handover_requested_by
        if state.lock_holder == session and state.handover_requested_by != session
        else None
    )
    previous = version.soap
    saved = record_clinical_note(
        clinic_id=clinic_id,
        version_id=version.pk,
        expected_revision=expected_revision,
        content=content,
    )
    epochs = dict(state.section_edit_epochs)
    for field in SOAP_FIELDS:
        if previous.get(field, "") != content[field]:
            epochs[field] = int(epochs[field]) + 1
    state.section_edit_epochs = epochs
    if handover_to is not None:
        # This session's text is saved first; the lock then moves.
        state.lock_holder = handover_to
        state.lock_expires_at = now + LOCK_TTL
        state.handover_requested_by = None
        record_phase1_event(
            "ehr.draft.handed_over", clinic_id=clinic_id, affected_record_id=version.pk
        )
    else:
        _hold(state, session, now)
    _save_state(state)
    DraftSaveReceipt.objects.create(
        organization_id=version.organization_id,
        version=version,
        command_id=command_id,
        request_sha256=digest,
        base_revision=expected_revision,
        revision=saved.revision,
        saved_at=now,
    )
    return AutosaveResult(
        status=AutosaveStatus.SAVED,
        revision=saved.revision,
        saved_at=now,
        handed_over=handover_to is not None,
    )


def autosave_draft(  # noqa: PLR0913 - the command's exact keyword contract
    *,
    clinic_id: UUID,
    version_id: UUID,
    expected_revision: int,
    editor_command_id: UUID,
    editor_session_id: UUID,
    sections: dict[str, str],
    request_handover: bool = False,
) -> AutosaveResult:
    """Run one autosave command; see the module contract."""
    content = _validated(
        expected_revision, editor_command_id, editor_session_id, sections
    )
    authorized = view_version(clinic_id=clinic_id, version_id=version_id)
    actor = _encounter_actor(clinic_id, authorized.document.encounter)
    if authorized.author_id != actor:
        _denied(clinic_id, version_id, "not_assigned")
    digest = request_digest(
        version_id=version_id, expected_revision=expected_revision, sections=content
    )
    now = utc_now()
    with transaction.atomic():
        # The version row lock serializes every command on this draft, so a
        # replay racing its original sees the committed receipt.
        version = (
            ClinicalDocumentVersion.objects.select_for_update()
            .filter(pk=version_id)
            .first()
        )
        if version is None or version.state != "draft":
            msg = "precondition_failed"
            raise ClinicalConflictError(msg)
        replayed = _replay(version, editor_command_id, digest)
        if replayed is not None:
            return replayed
        state = _edit_state(version)
        refused = _lock_refusal(
            state, version, editor_session_id, now, request_handover=request_handover
        )
        if refused is not None:
            return refused
        if version.revision != expected_revision:
            # The conflicting editor becomes the holder so its explicit merge
            # is not raced by further autosaves from the older session.
            _hold(state, editor_session_id, now)
            _save_state(state)
            return AutosaveResult(
                status=AutosaveStatus.CONFLICT,
                revision=version.revision,
                diff=section_diff(version.soap, content),
            )
        return _write(
            clinic_id,
            version,
            state,
            content,
            (editor_command_id, editor_session_id, expected_revision, digest),
            now,
        )


def section_edit_epochs(*, clinic_id: UUID, version_id: UUID) -> dict[str, int]:
    """Return the draft's acknowledged per-section edit counters (todo 42)."""
    authorized = view_version(clinic_id=clinic_id, version_id=version_id)
    actor = _encounter_actor(clinic_id, authorized.document.encounter)
    if authorized.author_id != actor:
        _denied(clinic_id, version_id, "not_assigned")
    state = DraftEditState.objects.filter(version_id=version_id).first()
    if state is None:
        return dict.fromkeys(SOAP_FIELDS, 0)
    return {field: int(state.section_edit_epochs[field]) for field in SOAP_FIELDS}
