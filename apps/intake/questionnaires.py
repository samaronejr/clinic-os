"""Versioned pre-consultation configuration and authorized response lifecycle.

Templates contain data only, never expressions or medical recommendations.
Database policies independently restrict sensitive rows to the bound patient
session or an active physician assigned to the exact clinic.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.identity.current_context import require_current_actor_clinic_roles
from apps.identity.models import Clinic, UserClinicRole
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import (
    PatientClinicEnrollment,
    QuestionnaireResponse,
    QuestionnaireTemplate,
)
from apps.scheduling.models import Appointment

if TYPE_CHECKING:
    from uuid import UUID

MAX_QUESTIONS = 40
MAX_TEXT = 4000
MAX_OPTIONS = 30
MAX_LABEL = 200
MAX_TITLE = 160
MAX_REASON = 255
QUESTION_KEYS = frozenset({"id", "label", "type", "required", "max_length", "options"})


def validate_questions(questions: object) -> list[dict[str, object]]:
    """Accept a small closed schema, not executable conditions or expressions."""
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        msg = "Use de 1 a 40 perguntas."
        raise ValidationError(msg)
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict) or set(question) != QUESTION_KEYS:
            msg = "Configuração de pergunta inválida."
            raise ValidationError(msg)
        key = question["id"]
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"q_[a-z0-9_]{1,32}", key)
            or key in seen
        ):
            msg = "Identificador de pergunta inválido ou repetido."
            raise ValidationError(msg)
        seen.add(key)
        label = question["label"]
        if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL:
            msg = "O rótulo deve ter de 1 a 200 caracteres."
            raise ValidationError(msg)
        kind = question["type"]
        if (
            kind not in ("text", "selection", "boolean")
            or type(question["required"]) is not bool
        ):
            msg = "Tipo ou obrigatoriedade inválidos."
            raise ValidationError(msg)
        length = question["max_length"]
        if type(length) is not int or not 1 <= length <= MAX_TEXT:
            msg = "O limite deve ser de 1 a 4000 caracteres."
            raise ValidationError(msg)
        _validate_options(kind, question["options"], length)
    return cast("list[dict[str, object]]", questions)


def _validate_options(kind: str, options: object, length: int) -> None:
    if not isinstance(options, list):
        message = "Opções inválidas."
        raise ValidationError(message)
    if kind == "selection":
        if (
            not 1 <= len(options) <= MAX_OPTIONS
            or any(
                not isinstance(option, str)
                or not option.strip()
                or len(option) > min(length, MAX_LABEL)
                for option in options
            )
            or len(set(options)) != len(options)
        ):
            message = "Use de 1 a 30 opções distintas dentro do limite."
            raise ValidationError(message)
    elif options:
        message = "Este tipo não aceita opções."
        raise ValidationError(message)


def validate_answers(
    questions: list[dict[str, object]], answers: object, *, submitting: bool
) -> dict[str, object]:
    """Validate supplied draft values; require all required values on submission."""
    if not isinstance(answers, dict) or set(answers) - {q["id"] for q in questions}:
        msg = "Respostas desconhecidas ou inválidas."
        raise ValidationError(msg)
    for question in questions:
        value = answers.get(question["id"])
        missing = value is None or value == ""
        if missing:
            if submitting and question["required"]:
                msg = "Preencha todas as perguntas obrigatórias."
                raise ValidationError(msg)
            continue
        if question["type"] == "boolean":
            valid = type(value) is bool
        else:
            valid = isinstance(value, str) and len(value) <= cast(
                "int", question["max_length"]
            )
            if question["type"] == "selection":
                valid = valid and value in cast("list[str]", question["options"])
            if submitting and question["required"] and isinstance(value, str):
                valid = valid and bool(value.strip())
        if not valid:
            msg = "Resposta inválida ou acima do limite de caracteres."
            raise ValidationError(msg)
    return cast("dict[str, object]", answers)


def publish_template(
    *, clinic_id: UUID, key: str, title: str, questions: object
) -> QuestionnaireTemplate:
    """Publish a new immutable version; serialize version allocation per clinic."""
    require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN)
    )
    validated = validate_questions(questions)
    if (
        not re.fullmatch(r"[a-z0-9_-]{1,64}", key)
        or not title.strip()
        or len(title) > MAX_TITLE
    ):
        msg = "Nome de configuração inválido."
        raise ValidationError(msg)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"questionnaire:{clinic_id}:{key}"],
        )
        clinic = Clinic.objects.get(pk=clinic_id)
        previous = (
            QuestionnaireTemplate.objects.filter(clinic=clinic, key=key)
            .order_by("-version")
            .first()
        )
        return QuestionnaireTemplate.objects.create(
            organization_id=clinic.organization_id,
            clinic=clinic,
            key=key,
            version=previous.version + 1 if previous else 1,
            title=title,
            questions=validated,
        )


def assign_questionnaire(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    template_id: UUID,
    appointment_id: UUID | None = None,
) -> QuestionnaireResponse:
    """Assign an exact version to an enrollment as its clinic physician."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    with transaction.atomic():
        enrollment = PatientClinicEnrollment.objects.filter(
            pk=enrollment_id, clinic_id=clinic_id
        ).first()
        template = QuestionnaireTemplate.objects.filter(
            pk=template_id, clinic_id=clinic_id
        ).first()
        if enrollment is None or template is None:
            raise PatientAccessDeniedError
        if appointment_id is not None:
            appointment: Appointment | None = Appointment.objects.filter(
                pk=appointment_id, clinic_id=clinic_id, patient_id=enrollment.patient_id
            ).first()
            if appointment is None:
                raise PatientAccessDeniedError
        return QuestionnaireResponse.objects.create(
            organization_id=enrollment.organization_id,
            clinic_id=clinic_id,
            patient_id=enrollment.patient_id,
            enrollment=enrollment,
            template=template,
            appointment_id=appointment_id,
        )


def published_templates(*, clinic_id: UUID) -> list[QuestionnaireTemplate]:
    """List the newest version of each configuration for a clinic physician."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    return list(
        QuestionnaireTemplate.objects.filter(clinic_id=clinic_id)
        .order_by("key", "-version")
        .distinct("key")
    )


def appointment_enrollment(*, clinic_id: UUID, appointment_id: UUID) -> UUID:
    """Resolve the enrollment behind one visible appointment of this clinic."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    appointment = Appointment.objects.filter(
        pk=appointment_id, clinic_id=clinic_id
    ).first()
    if appointment is None:
        raise PatientAccessDeniedError
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=appointment.patient_id
    ).first()
    if enrollment is None:
        raise PatientAccessDeniedError
    return enrollment.pk


def assign_for_appointment(
    *, clinic_id: UUID, appointment_id: UUID, template_id: UUID
) -> tuple[QuestionnaireResponse, bool]:
    """Assign once per appointment and configuration; a retry returns the first."""
    enrollment_id = appointment_enrollment(
        clinic_id=clinic_id, appointment_id=appointment_id
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"questionnaire-assign:{appointment_id}:{template_id}"],
        )
        existing = QuestionnaireResponse.objects.filter(
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            template_id=template_id,
        ).first()
        if existing is not None:
            return existing, False
        return (
            assign_questionnaire(
                clinic_id=clinic_id,
                enrollment_id=enrollment_id,
                template_id=template_id,
                appointment_id=appointment_id,
            ),
            True,
        )


def patient_responses() -> list[QuestionnaireResponse]:
    """List only rows authorized by the live patient session, never staff context."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.questionnaire_patient_enrollment()")
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise PatientAccessDeniedError
    return list(
        QuestionnaireResponse.objects.filter(enrollment_id=row[0])
        .select_related("template")
        .order_by("created_at", "pk")
    )


def patient_response(response_id: UUID) -> QuestionnaireResponse:
    """Reject swapped response identifiers without exposing their existence."""
    for response in patient_responses():
        if response.pk == response_id:
            return response
    raise PatientAccessDeniedError


def save_response(
    *, response_id: UUID, answers: object, expected_revision: int, submit: bool = False
) -> QuestionnaireResponse:
    """Save a patient draft or explicitly submit it; stale editors never overwrite."""
    authorized = patient_response(response_id)
    with transaction.atomic():
        response = QuestionnaireResponse.objects.select_for_update().get(
            pk=authorized.pk
        )
        if response.state != "draft" or response.revision != expected_revision:
            msg = "Este formulário mudou. Reabra a versão salva antes de continuar."
            raise ValidationError(msg)
        response.answers = validate_answers(
            response.template.questions, answers, submitting=submit
        )
        response.revision += 1
        if submit:
            response.state = "submitted"
            response.submitted_at = timezone.now()
        response.save(
            update_fields=("answers", "revision", "state", "submitted_at", "updated_at")
        )
        return response


def submit_intake(
    *, response_id: UUID, answers: object, expected_revision: int
) -> QuestionnaireResponse:
    """Explicitly submit a complete patient response to its retained version."""
    return save_response(
        response_id=response_id,
        answers=answers,
        expected_revision=expected_revision,
        submit=True,
    )


def clinical_response(*, clinic_id: UUID, response_id: UUID) -> QuestionnaireResponse:
    """Read sensitive answers only as a physician assigned to this clinic."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    response = (
        QuestionnaireResponse.objects.filter(clinic_id=clinic_id, pk=response_id)
        .select_related("template")
        .first()
    )
    if response is None:
        raise PatientAccessDeniedError
    return response


def reopen_response(
    *, clinic_id: UUID, response_id: UUID, reason: str, expected_revision: int
) -> QuestionnaireResponse:
    """Reopen as a physician with a reason while preserving immutable history."""
    if not reason.strip() or len(reason) > MAX_REASON:
        msg = "Informe um motivo de 1 a 255 caracteres."
        raise ValidationError(msg)
    with transaction.atomic():
        authorized = clinical_response(clinic_id=clinic_id, response_id=response_id)
        response = QuestionnaireResponse.objects.select_for_update().get(
            pk=authorized.pk
        )
        if response.state != "submitted" or response.revision != expected_revision:
            msg = "Somente a revisão enviada atual pode ser reaberta."
            raise ValidationError(msg)
        response.state = "draft"
        response.submitted_at = None
        response.reopen_reason = reason
        response.revision += 1
        response.save(
            update_fields=(
                "state",
                "submitted_at",
                "reopen_reason",
                "revision",
                "updated_at",
            )
        )
        return response


def completion_status(
    *, clinic_id: UUID, enrollment_id: UUID
) -> list[tuple[UUID, str, int]]:
    """Return only identity, completion state and revision through a narrow resolver."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.questionnaire_completion(%s, %s)",
            [clinic_id, enrollment_id],
        )
        return cast("list[tuple[UUID, str, int]]", cursor.fetchall())
