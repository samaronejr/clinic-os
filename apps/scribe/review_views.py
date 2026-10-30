"""Native, POST-only source/note/suggestion review; no refusal changes a session."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.ai.contracts import GatewayRefusedError
from apps.ehr.forms import SECTION_LABELS
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    record_clinical_note,
)
from apps.identity.otp import privileged_totp_required
from apps.scribe.contracts import ScribeError
from apps.scribe.draft_contracts import (
    SECTIONS,
    Decision,
    DraftAccessDeniedError,
    DraftConflictError,
    DraftInputError,
    DraftRequest,
    ReviewInput,
)
from apps.scribe.draft_sources import note_frame
from apps.scribe.drafting import request_draft
from apps.scribe.presenter import browser_binding
from apps.scribe.review_forms import ReviewCommand, ReviewValues
from apps.scribe.reviews import ReviewPanel, read_review, review_suggestion
from apps.scribe.views import continuation

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse


def _apply(values: ReviewValues, *, clinic_id: UUID) -> None:
    if values.action == "open":
        return
    revision = values.expected_revision
    if revision is None:
        raise DraftInputError
    if values.action == "refresh":
        frame = note_frame(clinic_id=clinic_id, version_id=values.version_id)
        if revision != frame.version.revision:
            raise DraftConflictError
    elif values.action == "generate":
        if values.transcript_id is None or values.request_key is None:
            raise DraftInputError
        request_draft(
            clinic_id=clinic_id,
            version_id=values.version_id,
            transcript_id=values.transcript_id,
            request_key=values.request_key,
            request=DraftRequest(
                revision, (values.section,) if values.section else SECTIONS
            ),
        )
    elif values.action == "save":
        record_clinical_note(
            clinic_id=clinic_id,
            version_id=values.version_id,
            expected_revision=revision,
            content=values.content,
        )
    elif values.suggestion_id is not None and values.command_id is not None:
        review_suggestion(
            clinic_id=clinic_id,
            version_id=values.version_id,
            review=ReviewInput(
                values.suggestion_id,
                revision,
                values.command_id,
                cast("Decision", values.action),
                values.content,
                values.replacement,
            ),
        )
    else:
        raise DraftInputError


def _context(
    panel: ReviewPanel, values: ReviewValues, *, clinic_id: UUID
) -> dict[str, object]:
    content = panel.version.soap
    if values.action in {"generate", "rejected", "refresh"}:
        content = values.content
    initial: dict[str, object] = {
        "encounter_id": values.encounter_id,
        "version_id": panel.version.pk,
        "expected_revision": panel.version.revision,
        "transcript_id": panel.transcript.version_id if panel.transcript else None,
        "request_key": uuid4(),
        "command_id": uuid4(),
        **content,
    }
    form = ReviewCommand(initial=initial)
    rows = []
    for row in panel.rows:
        links = [
            index + 1
            for reference in row.suggestion.citations
            for index, source in enumerate(panel.sources)
            if source.reference == reference
        ]
        rows.append(
            {
                "item": row,
                "label": SECTION_LABELS[row.suggestion.section],
                "links": links,
                "diff_id": f"compare-{row.suggestion.pk}",
                "lines": [
                    {"kind": "removed", "text": row.current},
                    {"kind": "added", "text": row.suggestion.text},
                ],
            }
        )
    return {
        "clinic_id": clinic_id,
        "encounter_id": values.encounter_id,
        "panel": panel,
        "form": form,
        "note_fields": [form[key] for key in SECTIONS],
        "rows": rows,
        "unsaved": content != panel.version.soap,
        "saved": values.action in {"save", "accepted", "modified", "rejected"},
        "queued": values.action == "generate",
    }


def _command(request: HttpRequest, clinic_id: UUID) -> ReviewValues:
    form = ReviewCommand(request.POST)
    if request.FILES or not form.is_valid():
        raise DraftInputError
    values = form.values()
    browser_binding(request, clinic_id=clinic_id, encounter_id=values.encounter_id)
    frame = note_frame(clinic_id=clinic_id, version_id=values.version_id)
    if frame.scope.encounter.pk != values.encounter_id:
        raise DraftAccessDeniedError
    return values


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_POST
def note_review(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Render a refusal outside the savepoint that contains every domain effect."""
    try:
        with transaction.atomic():
            values = _command(request, clinic_id)
            _apply(values, clinic_id=clinic_id)
            panel = read_review(clinic_id=clinic_id, version_id=values.version_id)
            context = _context(panel, values, clinic_id=clinic_id)
            return render(request, "scribe/review.html", context)
    except (DraftAccessDeniedError, ClinicalAccessDeniedError, ScribeError):
        context = {"clinic_id": clinic_id, "denied": True}
        status = 403
    except (DraftConflictError, ClinicalConflictError, GatewayRefusedError):
        context = {
            "clinic_id": clinic_id,
            "conflict": True,
            "submitted_note": [
                (SECTION_LABELS[section], text)
                for section, text in values.content.items()
            ],
            "error": _(
                "Nothing was applied. The note, sources or generation changed. "
                "Reopen the review and compare; your submitted text is preserved below."
            ),
        }
        status = 409
    except (DraftInputError, ValidationError):
        context = {"clinic_id": clinic_id, "invalid": True}
        status = 400
    return render(request, "scribe/review.html", context, status=status)
