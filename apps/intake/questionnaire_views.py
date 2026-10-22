"""Native, POST-body-only questionnaire screens in the patient and staff portals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from uuid import UUID

from django import forms
from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.core.patient_context import bind_patient_context
from apps.identity.current_context import CurrentActorError
from apps.identity.otp import privileged_totp_required
from apps.intake.access import PatientAccessDeniedError
from apps.intake.branching import MAX_NUMBER, definition_parts, visible_questions
from apps.intake.questionnaires import (
    answer_values,
    appointment_enrollment,
    assign_for_appointment,
    carry_forward_response,
    clinical_response,
    completion_status,
    patient_response,
    patient_responses,
    published_templates,
    reopen_response,
    save_response,
    unverified_answers,
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
        self.response = response
        questions, rules = definition_parts(response.template.questions)
        self.questions = questions
        values = answer_values(response)
        self.unverified = unverified_answers(response)
        super().__init__(
            data=cast("dict[str, str] | None", data),
            initial={
                "response_id": response.pk,
                "revision": response.revision,
                **values,
            },
        )
        for question in questions:
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
            elif question["type"] == "number":
                field = forms.FloatField(
                    label=label,
                    required=False,
                    min_value=-MAX_NUMBER,
                    max_value=MAX_NUMBER,
                    widget=forms.NumberInput(attrs={"step": "any"}),
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
            if question["id"] in self.unverified:
                self.fields[f"confirm_{question['id']}"] = forms.BooleanField(
                    label=_("I reviewed this carried-forward answer."),
                    required=False,
                )
        self.branch_definition = json.dumps(
            {
                "questions": [{"id": q["id"], "type": q["type"]} for q in questions],
                "rules": rules,
            }
        )

    def question_rows(self) -> list[dict[str, object]]:
        """Group each answer with its review checkbox and current server visibility."""
        values = answer_values(self.response)
        if self.is_bound:
            values = {}
            for question in self.questions:
                key = question["id"]
                raw = self.data.get(key, "")
                if question["type"] == "boolean":
                    values[key] = {"True": True, "False": False}.get(raw)
                elif question["type"] == "number":
                    try:
                        values[key] = float(raw) if raw else None
                    except ValueError:
                        values[key] = None
                else:
                    values[key] = raw
        visible = visible_questions(self.response.template.questions, values)
        return [
            {
                "id": q["id"],
                "field": self[q["id"]],
                "visible": q["id"] in visible,
                "confirmation": self[f"confirm_{q['id']}"]
                if q["id"] in self.unverified
                else None,
            }
            for q in self.questions
        ]

    def answers(self) -> dict[str, object]:
        """Return only fields from the template, never authority or revision inputs."""
        values = {
            key: value
            for key, value in self.cleaned_data.items()
            if key.startswith("q_")
        }
        if isinstance(self.response.template.questions, dict):
            return {
                "values": values,
                "confirmed": [
                    key
                    for key in self.unverified
                    if self.cleaned_data.get(f"confirm_{key}")
                ],
            }
        return values


def _denied(request: HttpRequest) -> HttpResponse:
    return render(request, "403.html", status=403)


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_questionnaires(request: HttpRequest) -> HttpResponse:
    """Select, resume, save and explicitly submit a form without identifiers in URLs."""
    response = None
    form = None
    notice = ""
    failure = ""
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
            elif action == "carry":
                response = carry_forward_response(
                    clinic_id=response.clinic_id,
                    response_id=response.pk,
                    source_response_id=UUID(request.POST.get("source_response_id", "")),
                    expected_revision=int(request.POST.get("revision", "0")),
                )
                form = ResponseForm(response)
                notice = _(
                    "Carried-forward answers are unverified. Review each answer."
                )
            elif action == "open":
                form = ResponseForm(response)
            else:
                return _denied(request)
        rows = patient_responses()
        sources = [
            row
            for row in rows
            if response is not None
            and isinstance(response.template.questions, dict)
            and not answer_values(response)
            and row.state == "submitted"
            and row.template_id == response.template_id
        ]
    except ValidationError as error:
        if response is not None:
            form = ResponseForm(response)
        failure = " ".join(error.messages)
        rows = patient_responses()
        sources = []
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
            "failure": failure,
            "carry_sources": sources,
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
    enrollment_id = appointment_enrollment(
        clinic_id=clinic_id, appointment_id=appointment_id
    )
    context["statuses"] = completion_status(
        clinic_id=clinic_id, enrollment_id=enrollment_id
    )
    bind_patient_context(request, clinic_id=clinic_id, enrollment_id=enrollment_id)
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
                enrollment_id = UUID(request.POST.get("enrollment_id", ""))
                context["statuses"] = completion_status(
                    clinic_id=clinic_id,
                    enrollment_id=enrollment_id,
                )
                bind_patient_context(
                    request, clinic_id=clinic_id, enrollment_id=enrollment_id
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
                bind_patient_context(
                    request, clinic_id=clinic_id, enrollment_id=response.enrollment_id
                )
                context["response"] = response
                context["answers"] = [
                    {
                        "label": q["label"],
                        "value": answer_values(response).get(q["id"], "—"),
                        "type": q["type"],
                        "unverified": q["id"] in unverified_answers(response),
                    }
                    for q in definition_parts(response.template.questions)[0]
                ]
            else:
                return _denied(request)
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
    except (ValueError, CurrentActorError, PatientAccessDeniedError):
        return _denied(request)
    return render(request, "intake/questionnaire_staff.html", context)
