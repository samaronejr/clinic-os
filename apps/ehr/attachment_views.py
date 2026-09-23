"""No-store attachment workspace; record selectors stay out of URLs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ehr.attachment_forms import AttachmentUploadForm
from apps.ehr.attachment_storage import AttachmentStorageError
from apps.ehr.attachments import (
    AttachmentInput,
    AttachmentScanFailedError,
    attachment_context,
    authorize_attachment_encounter,
    download_attachment,
    scan_attachment,
    upload_attachment,
)
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.ehr.models import Encounter

logger = logging.getLogger(__name__)
OK = 200


def continuation(clinic_id: UUID) -> str:
    """Authenticate without clinical identifiers in the continuation URL."""
    return reverse("ehr:attachments", kwargs={"clinic_id": clinic_id})


def _download(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Return a bounded, fully materialized response inside the transaction."""
    result = download_attachment(
        clinic_id=clinic_id, attachment_id=UUID(request.POST.get("attachment_id", ""))
    )
    response = HttpResponse(result.data, content_type=result.content_type)
    response["Content-Disposition"] = f'attachment; filename="{result.file_name}"'
    response["Content-Length"] = str(len(result.data))
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


def _scan(
    request: HttpRequest, clinic_id: UUID, key: str, encounter: Encounter
) -> HttpResponse:
    try:
        scan_attachment(
            clinic_id=clinic_id,
            attachment_id=UUID(request.POST.get("attachment_id", "")),
        )
    except AttachmentScanFailedError:
        messages.error(
            request,
            "A verificação não foi concluída. Tente novamente.",
            extra_tags="ehr.attachments.failed",
        )
    else:
        messages.success(
            request, "Verificação concluída.", extra_tags="ehr.attachments.saved"
        )
    request.session[key] = str(encounter.pk)
    return redirect(continuation(clinic_id))


def _upload(
    request: HttpRequest, clinic_id: UUID, encounter: Encounter
) -> tuple[AttachmentUploadForm, int, str]:
    """Validate and store one upload; keep the bound form on every failure."""
    form = AttachmentUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return form, 400, ""
    upload = form.cleaned_data["attachment"]
    try:
        upload_attachment(
            clinic_id=clinic_id,
            encounter_id=encounter.pk,
            upload=AttachmentInput(
                file_name=upload.name or "",
                declared_type=upload.content_type or "",
                data=upload.read(),
            ),
        )
    except ValidationError as error:
        form.add_error("attachment", error)
        return form, 400, ""
    except (DatabaseError, AttachmentStorageError):
        # Storage or database text can contain object details.
        logger.log(
            logging.ERROR, "ehr attachment upload failed; transaction rolled back"
        )
        return form, 503, "Não foi possível enviar. O arquivo não foi guardado."
    return form, OK, ""


def _workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    key = f"ehr.attachments.{clinic_id}"
    selected = (
        request.POST.get("encounter_id")
        if request.method == "POST"
        else request.session.get(key)
    )
    if not selected:
        if request.method == "POST":
            raise ClinicalAccessDeniedError
        return render(request, "ehr/attachments.html", {"clinic_id": clinic_id})
    encounter = authorize_attachment_encounter(clinic_id, UUID(str(selected)))
    form = None
    status = OK
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "open":
            request.session[key] = str(encounter.pk)
            return redirect(continuation(clinic_id))
        if action == "download":
            return _download(request, clinic_id)
        if action not in ("upload", "scan"):
            raise ClinicalAccessDeniedError
        authorize_attachment_encounter(clinic_id, encounter.pk, write=True)
        if action == "scan":
            return _scan(request, clinic_id, key, encounter)
        form, status, error = _upload(request, clinic_id, encounter)
        if status == OK:
            request.session[key] = str(encounter.pk)
            messages.success(
                request,
                "Arquivo enviado para verificação.",
                extra_tags="ehr.attachments.saved",
            )
            return redirect(continuation(clinic_id))
    context = attachment_context(encounter)
    if form is None and context.can_write:
        form = AttachmentUploadForm(initial={"encounter_id": encounter.pk})
    return render(
        request,
        "ehr/attachments.html",
        {
            "clinic_id": clinic_id,
            "encounter": encounter,
            "attachments": context.attachments,
            "can_write": context.can_write,
            "form": form,
            "error": error,
            "unsaved": status != OK,
        },
        status=status,
    )


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def attachment_workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Handle selection, upload, scan and download with closed failures."""
    try:
        return _workspace(request, clinic_id)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        return render(request, "403.html", status=403)
    except ClinicalConflictError:
        return render(request, "403.html", status=403)
    except AttachmentStorageError:
        logger.log(logging.ERROR, "ehr attachment storage failed")
        return render(
            request,
            "ehr/attachments.html",
            {
                "clinic_id": clinic_id,
                "error": "O arquivo não está disponível no momento.",
            },
            status=503,
        )
