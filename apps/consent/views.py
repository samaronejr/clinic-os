"""Patient decisions and staff read-only receipts; identifiers stay in POST bodies."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.consent.forms import (
    AcceptanceForm,
    AcknowledgmentForm,
    DisclosureForm,
    NoticeForm,
    RefusalForm,
    TextForm,
)
from apps.consent.services import (
    STAFF_ROLES,
    acknowledge_participant,
    available_notices,
    available_texts,
    patient_receipts,
    patient_refusals,
    prepare_acceptance,
    publish_notice,
    publish_text,
    record_ai_disclosure,
    record_consent,
    record_refusal,
    revoke_consent,
    staff_receipts,
    staff_refusals,
)
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.otp import privileged_totp_required
from apps.intake.access import PatientAccessDeniedError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    return response


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_consent(request: HttpRequest) -> HttpResponse:
    """Read, explicitly accept, refuse, inspect and revoke only this patient's."""
    context: dict[str, object] = {}
    status = 200
    try:
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "read":
                text, token = prepare_acceptance(
                    text_id=UUID(request.POST.get("text_id", ""))
                )
                context.update(
                    text=text,
                    form=AcceptanceForm(
                        initial={"offer": token, "purpose": text.purpose}
                    ),
                    refusal_form=RefusalForm(
                        initial={"offer": token, "purpose": text.purpose},
                        prefix="refusal",
                    ),
                )
            elif action == "accept":
                form = AcceptanceForm(request.POST)
                if form.is_valid():
                    record_consent(**form.cleaned_data)
                    context["notice"] = (
                        "Consentimento registrado. Guarde seu comprovante abaixo."
                    )
                else:
                    context["error"] = (
                        "Nenhum consentimento registrado. Leia o texto e marque "
                        "a opção somente se desejar aceitar."
                    )
                    status = 400
            elif action == "refuse":
                refusal_form = RefusalForm(request.POST, prefix="refusal")
                if refusal_form.is_valid():
                    record_refusal(**refusal_form.cleaned_data)
                    context["notice"] = (
                        "Recusa registrada. O atendimento não depende desta "
                        "autorização e seu histórico foi preservado."
                    )
                else:
                    context["error"] = (
                        "Nenhuma recusa registrada. Leia o texto da versão atual."
                    )
                    status = 400
            elif action == "revoke":
                revoke_consent(
                    acceptance_id=UUID(request.POST.get("acceptance_id", ""))
                )
                context["notice"] = (
                    "Consentimento revogado. O histórico clínico e o comprovante "
                    "foram preservados."
                )
            else:
                return _private(render(request, "consent/denied.html", status=403))
        context["texts"] = available_texts()
        context["notices"] = available_notices()
        context["receipts"] = patient_receipts()
        context["refusals"] = patient_refusals()
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
        context["texts"] = available_texts()
        context["notices"] = available_notices()
        context["receipts"] = patient_receipts()
        context["refusals"] = patient_refusals()
        status = 409
    except (PatientAccessDeniedError, ValueError):
        return _private(render(request, "consent/denied.html", status=403))
    return _private(render(request, "consent/patient.html", context, status=status))


def _continuation(clinic_id: UUID) -> str:
    return reverse("consent:staff", kwargs={"clinic_id": clinic_id})


def _staff_forms() -> dict[str, object]:
    return {
        "form": TextForm(),
        "notice_form": NoticeForm(prefix="notice"),
        "disclosure_form": DisclosureForm(prefix="disclosure"),
        "acknowledgment_form": AcknowledgmentForm(prefix="ack"),
    }


def _publish_version(
    request: HttpRequest, context: dict[str, object], clinic_id: UUID
) -> HTTPStatus:
    form = TextForm(request.POST)
    context["form"] = form
    if not form.is_valid():
        return HTTPStatus.BAD_REQUEST
    published = publish_text(clinic_id=clinic_id, **form.cleaned_data)
    context["notice"] = (
        f"Versão {published.version} publicada. Os textos anteriores foram preservados."
    )
    context["form"] = TextForm()
    return HTTPStatus.OK


def _publish_notice_version(
    request: HttpRequest, context: dict[str, object], clinic_id: UUID
) -> HTTPStatus:
    form = NoticeForm(request.POST, prefix="notice")
    context["notice_form"] = form
    if not form.is_valid():
        return HTTPStatus.BAD_REQUEST
    published = publish_notice(clinic_id=clinic_id, **form.cleaned_data)
    context["notice"] = (
        f"Aviso versão {published.version} publicado. "
        "Avisos informam e não substituem consentimento."
    )
    context["notice_form"] = NoticeForm(prefix="notice")
    return HTTPStatus.OK


def _record_disclosure(
    request: HttpRequest, context: dict[str, object], clinic_id: UUID
) -> HTTPStatus:
    form = DisclosureForm(request.POST, prefix="disclosure")
    context["disclosure_form"] = form
    if not form.is_valid():
        return HTTPStatus.BAD_REQUEST
    record_ai_disclosure(clinic_id=clinic_id, **form.cleaned_data)
    context["notice"] = "Divulgação de uso de IA registrada para o atendimento."
    context["disclosure_form"] = DisclosureForm(prefix="disclosure")
    return HTTPStatus.OK


def _record_acknowledgment(
    request: HttpRequest, context: dict[str, object], clinic_id: UUID
) -> HTTPStatus:
    form = AcknowledgmentForm(request.POST, prefix="ack")
    context["acknowledgment_form"] = form
    if not form.is_valid():
        return HTTPStatus.BAD_REQUEST
    acknowledge_participant(clinic_id=clinic_id, **form.cleaned_data)
    context["notice"] = "Aviso de gravação registrado para o participante."
    context["acknowledgment_form"] = AcknowledgmentForm(prefix="ack")
    return HTTPStatus.OK


def _staff_action(
    request: HttpRequest, context: dict[str, object], clinic_id: UUID
) -> HTTPStatus:
    """Run one staff action and return its HTTP status."""
    action = request.POST.get("action")
    handlers = {
        "publish": _publish_version,
        "publish_notice": _publish_notice_version,
        "disclosure": _record_disclosure,
        "acknowledge": _record_acknowledgment,
    }
    handler = handlers.get(action or "")
    if handler is not None:
        return handler(request, context, clinic_id)
    if action != "receipts":
        return HTTPStatus.FORBIDDEN
    enrollment_id = UUID(request.POST.get("enrollment_id", ""))
    context["receipts"] = staff_receipts(
        clinic_id=clinic_id, enrollment_id=enrollment_id
    )
    context["refusals"] = staff_refusals(
        clinic_id=clinic_id, enrollment_id=enrollment_id
    )
    return HTTPStatus.OK


@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def staff_consent(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Publish overlays and notices, record disclosures; never act for patients."""
    context: dict[str, object] = {**_staff_forms(), "clinic_id": clinic_id}
    status = HTTPStatus.OK
    try:
        require_current_actor_clinic_roles(clinic_id, STAFF_ROLES)
        if request.method == "POST":
            status = _staff_action(request, context, clinic_id)
            if status == HTTPStatus.FORBIDDEN:
                return _private(
                    render(request, "403.html", status=HTTPStatus.FORBIDDEN)
                )
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
        status = HTTPStatus.BAD_REQUEST
    except (PatientAccessDeniedError, CurrentActorError, ValueError):
        return _private(render(request, "403.html", status=HTTPStatus.FORBIDDEN))
    return _private(render(request, "consent/staff.html", context, status=status.value))
