"""Native, no-store clinical documents; record selection stays in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.core.patient_context import bind_encounter_context
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.identity.otp import privileged_totp_required
from apps.prescription.document_forms import DocumentActionForm, DocumentContentForm
from apps.prescription.document_rendering import DocumentRenderingError
from apps.prescription.models import (
    DocumentDraftContent,
    DocumentKind,
    PrescriptionDocument,
    PrescriptionDraft,
)
from apps.prescription.services import (
    authorize_document_draft,
    create_document_draft,
    discard_draft,
    render_document,
    save_document_draft,
)
from apps.prescription.views import (
    OK,
    ActionOutcome,
    _document_lifecycle_action,
    _document_rows,
    _screen,
    _subject,
    review_continuation,
)

if TYPE_CHECKING:
    from django.http import HttpRequest


def document_continuation(clinic_id: UUID) -> str:
    """Return to the selected document without putting its identity in a URL."""
    return reverse("prescription:clinical-documents", args=(clinic_id,))


def _selected_draft(
    request: HttpRequest, clinic_id: UUID, action: str
) -> PrescriptionDraft | HttpResponseBase | None:
    """Resolve a canonical selection before writing any draft or session."""
    key = f"prescription.clinical-document.{clinic_id}"
    if action == "open":
        kind = request.POST.get("kind", "")
        if kind not in DocumentKind.values or kind == DocumentKind.PRESCRIPTION:
            raise ValidationError(_("Choose a document kind."))
        draft = create_document_draft(
            clinic_id=clinic_id,
            encounter_id=UUID(request.POST.get("encounter_id", "")),
            kind=kind,
        )
        request.session[key] = str(draft.pk)
        return redirect(document_continuation(clinic_id))
    selected = (
        request.POST.get("draft_id")
        if request.method == "POST"
        else request.session.get(key)
    )
    if not selected:
        if request.method == "POST":
            raise ClinicalAccessDeniedError
        return None
    draft = authorize_document_draft(clinic_id=clinic_id, draft_id=UUID(str(selected)))
    if draft.kind == DocumentKind.PRESCRIPTION:
        raise ClinicalAccessDeniedError
    if action == "open_draft":
        request.session[key] = str(draft.pk)
        return redirect(document_continuation(clinic_id))
    return draft


def _frozen_action(
    request: HttpRequest, clinic_id: UUID, draft: PrescriptionDraft, action: str
) -> HttpResponseBase:
    """Render or discard only the submitted saved revision."""
    command = DocumentActionForm(request.POST)
    if not command.is_valid():
        raise ValidationError(_("Check the clinical document fields."))
    version = command.cleaned_data["expected_version"]
    if draft.version != version:
        reason = "stale_revision"
        raise ClinicalConflictError(reason)
    if action == "discard":
        discard_draft(
            clinic_id=clinic_id,
            draft_id=draft.pk,
            expected_version=version,
            encounter_id=draft.encounter_id,
            patient_id=draft.patient_id,
            issuer_id=draft.issuer_id,
        )
        return redirect(document_continuation(clinic_id))
    existing = PrescriptionDocument.objects.filter(
        draft=draft, document_version=version
    ).first()
    document = (
        existing
        if existing is not None
        else render_document(
            clinic_id=clinic_id,
            draft_id=draft.pk,
            expected_version=version,
            encounter_id=draft.encounter_id,
            patient_id=draft.patient_id,
            issuer_id=draft.issuer_id,
        )
    )
    return redirect(review_continuation(clinic_id, document.pk))


def _apply_action(
    request: HttpRequest,
    draft: PrescriptionDraft,
    form: DocumentContentForm,
    action: str,
) -> HttpResponseBase | ActionOutcome:
    """Dispatch an explicit command without concealing its failure."""
    clinic_id = draft.clinic_id
    match action:
        case "save":
            if not form.is_valid():
                return ActionOutcome(400, "", "invalid_document")
            save_document_draft(
                clinic_id=clinic_id,
                draft_id=draft.pk,
                expected_version=form.cleaned_data["expected_version"],
                content=form.content(),
            )
            messages.success(
                request,
                _("Document draft saved."),
                extra_tags="clinical-document-saved",
            )
            return redirect(document_continuation(clinic_id))
        case "render" | "discard":
            return _frozen_action(request, clinic_id, draft, action)
        case (
            "release_document"
            | "revoke_release"
            | "deliver_document"
            | "revoke_document"
        ):
            if not PrescriptionDocument.objects.filter(
                pk=UUID(request.POST.get("document_id", "")), draft=draft
            ).exists():
                raise ClinicalAccessDeniedError
            result = _document_lifecycle_action(
                request, clinic_id, draft.encounter, action
            )
            if isinstance(result, ActionOutcome):
                return result
            return redirect(document_continuation(clinic_id))
        case _:
            raise ClinicalAccessDeniedError


def _workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    action = request.POST.get("action", "") if request.method == "POST" else ""
    draft = _selected_draft(request, clinic_id, action)
    if isinstance(draft, HttpResponseBase):
        return draft
    if draft is None:
        return render(
            request, "prescription/clinical_documents.html", {"clinic_id": clinic_id}
        )
    encounter = draft.encounter
    bind_encounter_context(request, encounter)
    snapshot = DocumentDraftContent.objects.filter(
        draft=draft, version=draft.version
    ).first()
    initial = {
        **(snapshot.content if snapshot else {}),
        "expected_version": draft.version,
    }
    form = DocumentContentForm(
        request.POST if action == "save" else None, initial=initial
    )
    outcome = None
    status = 200
    try:
        if request.method == "POST":
            result = _apply_action(request, draft, form, action)
            if isinstance(result, ActionOutcome):
                outcome, status = result, result.status
            else:
                return result
    except ClinicalConflictError as error:
        status = 409
        outcome = ActionOutcome(
            status,
            _("The saved version changed. Reopen it before continuing."),
            error.reason_code,
        )
    except ValidationError:
        status = 400
        form.add_error(None, _("Check the clinical document fields."))
    except DocumentRenderingError:
        status = 503
        outcome = ActionOutcome(
            status,
            _("Rendering is unavailable. No document or order was created."),
            "rendering_unavailable",
        )
    return render(
        request,
        "prescription/clinical_documents.html",
        {
            **_screen(clinic_id, encounter, _subject(encounter)),
            "draft": draft,
            "form": form,
            "documents": _document_rows(draft, clinic_id),
            "is_exam_request": draft.kind == DocumentKind.EXAM_REQUEST,
            "error": outcome.error if outcome else "",
            "error_code": outcome.code if outcome else "",
            "unsaved": action == "save" and status != OK,
            "step": "author",
        },
        status=status,
    )


@never_cache
@privileged_totp_required(document_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def clinical_documents_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Recheck authority on every request; refused selectors reveal no content."""
    try:
        return _workspace(request, clinic_id)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        return render(request, "403.html", status=403)
    except (ValidationError, ClinicalConflictError):
        return render(
            request,
            "prescription/clinical_documents.html",
            {
                "clinic_id": clinic_id,
                "error": _(
                    "The document could not be opened. Check the encounter and kind."
                ),
            },
            status=400,
        )
