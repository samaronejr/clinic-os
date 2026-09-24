"""No-store longitudinal workspace; record selectors remain out of URLs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ehr.history import (
    HistoryChange,
    authorize_history,
    read_history,
    save_history,
)
from apps.ehr.history_forms import HistoryForm
from apps.ehr.models import HistoryAssessment
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from apps.ehr.models import Encounter

logger = logging.getLogger(__name__)
OK = 200


def continuation(clinic_id: UUID) -> str:
    """Authenticate without clinical identifiers in the continuation URL."""
    return reverse("ehr:history", kwargs={"clinic_id": clinic_id})


def _editor(request: HttpRequest, encounter: Encounter) -> HistoryForm:
    kind = request.POST.get("kind", "")
    if kind not in HistoryAssessment.Kind.values:
        raise ClinicalAccessDeniedError
    context = read_history(
        clinic_id=encounter.clinic_id, encounter_id=encounter.pk, kind=kind
    )
    initial: dict[str, object] = {
        "encounter_id": encounter.pk,
        "kind": kind,
        "revision": context.revision,
        "state": "documented",
        "status": "active",
    }
    if request.POST.get("action") == "edit":
        entry_id = UUID(request.POST.get("entry_id", ""))
        entry = next((e for e in context.entries if e.entry_id == entry_id), None)
        if entry is None:
            raise ClinicalAccessDeniedError
        authorize_history(
            encounter.clinic_id, entry.assessment.encounter_id, write=True
        )
        initial.update(
            entry_id=entry.entry_id, description=entry.description, status=entry.status
        )
    return HistoryForm(initial=initial)


def _save(request: HttpRequest, encounter: Encounter) -> tuple[HistoryForm, int]:
    form = HistoryForm(request.POST)
    if not form.is_valid():
        return form, 400
    values = form.cleaned_data
    try:
        save_history(
            clinic_id=encounter.clinic_id,
            encounter_id=encounter.pk,
            change=HistoryChange(
                kind=values["kind"],
                expected_revision=values["revision"],
                state=values["state"],
                description=values["description"],
                status=values["status"],
                reason=values["reason"],
                entry_id=values["entry_id"],
            ),
        )
    except ClinicalConflictError as error:
        form.add_error(
            None,
            "A versão mudou. Alterações não salvas; reabra o contexto atual."
            if error.reason_code == "stale_revision"
            else "Há registros preservados. Corrija ou resolva cada registro; "
            "não declare ausência.",
        )
        return form, 409
    except ValidationError as error:
        form.add_error(None, error)
        return form, 400
    except DatabaseError:
        # Database exception text can contain clinical values; do not log it.
        logger.log(logging.ERROR, "ehr history save failed; transaction rolled back")
        form.add_error(
            None, "Não foi possível salvar. Suas alterações não foram salvas."
        )
        return form, 503
    return form, OK


def _workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    key = f"ehr.history.{clinic_id}"
    selected = (
        request.POST.get("encounter_id")
        if request.method == "POST"
        else request.session.get(key)
    )
    if not selected:
        if request.method == "POST":
            raise ClinicalAccessDeniedError
        return render(request, "ehr/history.html", {"clinic_id": clinic_id})
    encounter = authorize_history(clinic_id, UUID(str(selected)))
    form = None
    status = OK
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "open":
            request.session[key] = str(encounter.pk)
            return redirect(continuation(clinic_id))
        if action not in ("save", "edit", "new"):
            raise ClinicalAccessDeniedError
        authorize_history(clinic_id, encounter.pk, write=True)
        if action in ("new", "edit"):
            form = _editor(request, encounter)
        else:
            form, status = _save(request, encounter)
            if status == OK:
                request.session[key] = str(encounter.pk)
                messages.success(
                    request, "Histórico salvo.", extra_tags="ehr.history.saved"
                )
                return redirect(continuation(clinic_id))
    sections = [
        read_history(clinic_id=clinic_id, encounter_id=encounter.pk, kind=kind)
        for kind in HistoryAssessment.Kind.values
    ]
    return render(
        request,
        "ehr/history.html",
        {
            "clinic_id": clinic_id,
            "encounter": encounter,
            "sections": sections,
            "form": form,
            "unsaved": status != OK,
        },
        status=status,
    )


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def history_workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Handle selection and updates with non-enumerating authorization failures."""
    try:
        return _workspace(request, clinic_id)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        return render(request, "403.html", status=403)
