"""Native, POST-body-only questionnaire screens in the patient and staff portals."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID

from django import forms
from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.identity.current_context import CurrentActorError
from apps.identity.otp import privileged_totp_required
from apps.intake.access import PatientAccessDeniedError
from apps.intake.questionnaires import (
    appointment_enrollment,
    assign_for_appointment,
    clinical_response,
    completion_status,
    patient_response,
    patient_responses,
    published_templates,
    reopen_response,
    save_response,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from apps.intake.models import QuestionnaireResponse


class ResponseForm(forms.Form):
    """Build controls exclusively from the retained immutable schema."""

    response_id = forms.UUIDField(widget=forms.HiddenInput)
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, response: QuestionnaireResponse, data: object = None) -> None:
        """Render optional draft fields; submission completeness is server validated."""
        super().__init__(
            data=cast("dict[str, str] | None", data),
            initial={
                "response_id": response.pk,
                "revision": response.revision,
                **response.answers,
            },
        )
        for question in response.template.questions:
            label = question["label"] + (
                " (obrigatório)" if question["required"] else " (opcional)"
            )
            if question["type"] == "text":
                field: forms.Field = forms.CharField(
                    label=label,
                    required=False,
                    max_length=question["max_length"],
                    widget=forms.Textarea(attrs={"rows": 3}),
                    help_text=f"Até {question['max_length']} caracteres.",
                    strip=False,
                )
            elif question["type"] == "selection":
                field = forms.ChoiceField(
                    label=label,
                    required=False,
                    choices=[("", "Selecione")] + [(v, v) for v in question["options"]],
                )
            else:
                field = forms.TypedChoiceField(
                    label=label,
                    required=False,
                    choices=[("", "Selecione"), ("True", "Sim"), ("False", "Não")],
                    coerce=lambda value: value == "True",
                    empty_value=None,
                )
            self.fields[question["id"]] = field

    def answers(self) -> dict[str, object]:
        """Return only fields from the template, never authority or revision inputs."""
        return {
            key: value
            for key, value in self.cleaned_data.items()
            if key.startswith("q_")
        }


def _denied(request: HttpRequest) -> HttpResponse:
    return render(request, "403.html", status=403)


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_questionnaires(request: HttpRequest) -> HttpResponse:
    """Select, resume, save and explicitly submit a form without identifiers in URLs."""
    response = None
    form = None
    notice = ""
    try:
        if request.method == "POST":
            response = patient_response(UUID(request.POST.get("response_id", "")))
            action = request.POST.get("action")
            if action in ("save", "submit"):
                form = ResponseForm(response, request.POST)
                if form.is_valid():
                    try:
                        response = save_response(
                            response_id=response.pk,
                            answers=form.answers(),
                            expected_revision=form.cleaned_data["revision"],
                            submit=action == "submit",
                        )
                        notice = (
                            "Formulário enviado."
                            if action == "submit"
                            else "Rascunho salvo. Você pode continuar depois."
                        )
                        form = ResponseForm(response)
                    except ValidationError as error:
                        form.add_error(None, error)
            elif action == "open":
                form = ResponseForm(response)
            else:
                return _denied(request)
        rows = patient_responses()
    except (ValueError, PatientAccessDeniedError):
        return _denied(request)
    return render(
        request,
        "intake/questionnaires.html",
        {
            "rows": rows,
            "response": response,
            "form": form,
            "notice": notice,
        },
    )


def _appointment_context(
    request: HttpRequest, clinic_id: UUID, action: str
) -> dict[str, object]:
    """Physician view of one appointment: its forms plus the assign control."""
    appointment_id = UUID(request.POST.get("appointment_id", ""))
    context: dict[str, object] = {}
    if action == "assign":
        _, created = assign_for_appointment(
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            template_id=UUID(request.POST.get("template_id", "")),
        )
        context["notice"] = (
            "Questionário atribuído. O paciente o encontra no portal."
            if created
            else "Este questionário já estava atribuído a esta consulta."
        )
    context["statuses"] = completion_status(
        clinic_id=clinic_id,
        enrollment_id=appointment_enrollment(
            clinic_id=clinic_id, appointment_id=appointment_id
        ),
    )
    context["appointment_id"] = appointment_id
    context["templates"] = published_templates(clinic_id=clinic_id)
    return context


def staff_continuation(clinic_id: UUID) -> str:
    """Return a safe GET with no patient state after authentication."""
    return reverse("intake:questionnaire-staff", kwargs={"clinic_id": clinic_id})


@privileged_totp_required(staff_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def staff_questionnaires(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Reception sees status only; physicians inspect and explicitly reopen answers."""
    context: dict[str, object] = {}
    try:
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "status":
                context["statuses"] = completion_status(
                    clinic_id=clinic_id,
                    enrollment_id=UUID(request.POST.get("enrollment_id", "")),
                )
            elif action in ("appointment", "assign"):
                context.update(_appointment_context(request, clinic_id, action))
            elif action in ("inspect", "reopen"):
                response_id = UUID(request.POST.get("response_id", ""))
                if action == "reopen":
                    reopen_response(
                        clinic_id=clinic_id,
                        response_id=response_id,
                        reason=request.POST.get("reason", ""),
                        expected_revision=int(request.POST.get("revision", "0")),
                    )
                    context["notice"] = (
                        "Formulário reaberto para o paciente. "
                        "O envio anterior foi preservado."
                    )
                response = clinical_response(
                    clinic_id=clinic_id, response_id=response_id
                )
                context["response"] = response
                context["answers"] = [
                    (q["label"], response.answers.get(q["id"], "—"))
                    for q in response.template.questions
                ]
            else:
                return _denied(request)
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
    except (ValueError, CurrentActorError, PatientAccessDeniedError):
        return _denied(request)
    return render(request, "intake/questionnaire_staff.html", context)
