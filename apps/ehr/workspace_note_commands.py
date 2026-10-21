"""Native restricted-note, copy-review and contribution command adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.ehr.addenda import autosave_addendum, open_addendum
from apps.ehr.note_access import restrict_note
from apps.ehr.prior_notes import CopyNoteCommand, reuse_prior_note, review_copied_block
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError

if TYPE_CHECKING:
    from django.http import QueryDict


def note_command(*, clinic_id: UUID, data: QueryDict) -> None:
    """Require explicit review and preserve the existing addendum CAS protocol."""
    match data.get("action"):
        case "restrict":
            restrict_note(
                clinic_id=clinic_id, version_id=UUID(data.get("version_id", ""))
            )
        case "copy":
            reuse_prior_note(
                clinic_id=clinic_id,
                target_version_id=UUID(data.get("target_version_id", "")),
                source_version_id=UUID(data.get("source_version_id", "")),
                command=CopyNoteCommand(
                    section=data.get("section", ""),
                    expected_revision=int(data.get("revision", "")),
                    command_id=UUID(data.get("command_id", "")),
                ),
            )
        case "review_copy":
            if data.get("reviewed") != "yes":
                raise ValidationError(_("Confirm that you reviewed the copied block."))
            review_copied_block(
                clinic_id=clinic_id,
                block_id=UUID(data.get("block_id", "")),
                expected_revision=int(data.get("revision", "")),
            )
        case "open_addendum":
            open_addendum(
                clinic_id=clinic_id, encounter_id=UUID(data.get("encounter_id", ""))
            )
        case "save_addendum":
            result = autosave_addendum(
                clinic_id=clinic_id,
                addendum_id=UUID(data.get("addendum_id", "")),
                expected_revision=int(data.get("revision", "")),
                editor_command_id=UUID(data.get("command_id", "")),
                text=data.get("text", ""),
            )
            if result.status not in {"saved", "replayed"}:
                reason = "stale_revision"
                raise ClinicalConflictError(reason)
        case _:
            raise ClinicalAccessDeniedError
