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
from django.utils.translation import gettext as _

from apps.audit.services import record_phase1_event
from apps.identity.current_context import require_current_actor_clinic_roles
from apps.identity.models import Clinic, UserClinicRole
from apps.intake.access import PatientAccessDeniedError
from apps.intake.branching import (
    definition_parts,
    is_number,
    validate_definition,
    visible_questions,
)
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
            kind not in ("text", "selection", "boolean", "number")
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
    questions: object, answers: object, *, submitting: bool
) -> dict[str, object]:
    """Validate types first, discard hidden answers and require only visible fields."""
    definition = questions
    rows, _rules = definition_parts(definition)
    if not isinstance(answers, dict) or set(answers) - {q["id"] for q in rows}:
        msg = "Respostas desconhecidas ou inválidas."
        raise ValidationError(msg)
    for question in rows:
        value = answers.get(question["id"])
        missing = value is None or value == ""
        if missing:
            continue
        if question["type"] == "boolean":
            valid = type(value) is bool
        elif question["type"] == "number":
            valid = is_number(value)
        else:
            valid = isinstance(value, str) and len(value) <= question["max_length"]
            if question["type"] == "selection":
                valid = valid and value in question["options"]
        if not valid:
            msg = "Resposta inválida ou acima do limite de caracteres."
            raise ValidationError(msg)
    visible = visible_questions(definition, answers)
    if submitting and any(
        q["required"]
        and (
            answers.get(q["id"]) is None
            or (
                isinstance(answers.get(q["id"]), str)
                and not cast("str", answers[q["id"]]).strip()
            )
        )
        for q in rows
        if q["id"] in visible
    ):
        raise ValidationError(_("Complete all visible required questions."))
    return {
        key: value
        for key, value in answers.items()
        if key in visible
        and (isinstance(definition, list) or (value is not None and value != ""))
    }


def publish_template(
    *, clinic_id: UUID, key: str, title: str, questions: object
) -> QuestionnaireTemplate:
    """Publish a new immutable version; serialize version allocation per clinic."""
    require_current_actor_clinic_roles(
        clinic_id,
        (
            UserClinicRole.Role.OWNER,
            UserClinicRole.Role.CLINIC_ADMIN,
            UserClinicRole.Role.PHYSICIAN,
        ),
    )
    if isinstance(questions, dict):
        validate_definition(questions)
        rows, _rules = definition_parts(questions)
        validate_questions(rows)
        validated: object = questions
    else:
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
        template = QuestionnaireTemplate.objects.create(
            organization_id=clinic.organization_id,
            clinic=clinic,
            key=key,
            version=previous.version + 1 if previous else 1,
            title=title,
            questions=validated,
        )
        state = validated["state"] if isinstance(validated, dict) else "published"
        event_type = {
            "draft": "intake.questionnaire_template.draft_created",
            "published": "intake.questionnaire_template.published",
            "retired": "intake.questionnaire_template.retired",
        }[state]
        record_phase1_event(
            event_type, clinic_id=clinic_id, affected_record_id=template.pk
        )
        return template


def assign_questionnaire(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    template_id: UUID,
    appointment_id: UUID | None = None,
    restricted_from_delegates: bool = True,
) -> QuestionnaireResponse:
    """Assign an exact version to an enrollment as its clinic physician."""
    if type(restricted_from_delegates) is not bool:
        raise PatientAccessDeniedError
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    with transaction.atomic():
        enrollment = PatientClinicEnrollment.objects.filter(
            pk=enrollment_id, clinic_id=clinic_id
        ).first()
        template = QuestionnaireTemplate.objects.filter(
            pk=template_id, clinic_id=clinic_id
        ).first()
        if (
            enrollment is None
            or template is None
            or (
                isinstance(template.questions, dict)
                and template.questions["state"] != "published"
            )
        ):
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
            restricted_from_delegates=restricted_from_delegates,
        )


def published_templates(*, clinic_id: UUID) -> list[QuestionnaireTemplate]:
    """List the newest version of each configuration for a clinic physician."""
    from apps.ehr.pack_models import SpecialtyPack  # noqa: PLC0415

    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    latest = list(
        QuestionnaireTemplate.objects.filter(clinic_id=clinic_id)
        .exclude(
            pk__in=SpecialtyPack.objects.filter(
                clinic_id=clinic_id, uninstalled_at__isnull=False
            ).values("questionnaire_id")
        )
        .order_by("key", "-version")
        .distinct("key")
    )
    return [
        template
        for template in latest
        if isinstance(template.questions, list)
        or template.questions["state"] == "published"
    ]


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
        if isinstance(response.template.questions, dict):
            if (
                not isinstance(answers, dict)
                or set(answers) != {"values", "confirmed"}
                or not isinstance(answers["confirmed"], list)
                or any(not isinstance(key, str) for key in answers["confirmed"])
                or set(answers["confirmed"]) - set(unverified_answers(response))
            ):
                raise ValidationError(_("Invalid answer confirmation."))
            values = validate_answers(
                response.template.questions, answers["values"], submitting=submit
            )
            previous = answer_values(response)
            unverified = [
                key
                for key in unverified_answers(response)
                if key in values
                and values[key] == previous.get(key)
                and key not in answers["confirmed"]
            ]
            if submit and unverified:
                raise ValidationError(
                    _("Review carried-forward answers before submitting.")
                )
            response.answers = {"values": values, "unverified": unverified}
        else:
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


def answer_values(response: QuestionnaireResponse) -> dict[str, object]:
    """Return clinical values separately from carried-forward provenance."""
    if isinstance(response.template.questions, dict):
        return cast("dict[str, object]", response.answers.get("values", {}))
    return cast("dict[str, object]", response.answers)


def unverified_answers(response: QuestionnaireResponse) -> list[str]:
    """Retain explicit unverified flags inside the existing encrypted envelope."""
    if isinstance(response.template.questions, dict):
        return cast("list[str]", response.answers.get("unverified", []))
    return []


def carry_forward_response(
    *,
    clinic_id: UUID,
    response_id: UUID,
    source_response_id: UUID,
    expected_revision: int,
) -> QuestionnaireResponse:
    """Copy only the patient's own submitted exact template, explicitly unverified."""
    authorized = patient_response(response_id)
    source = patient_response(source_response_id)
    if (
        authorized.clinic_id != clinic_id
        or source.clinic_id != clinic_id
        or authorized.enrollment_id != source.enrollment_id
        or authorized.template_id != source.template_id
        or not isinstance(authorized.template.questions, dict)
        or source.state != "submitted"
    ):
        raise PatientAccessDeniedError
    with transaction.atomic():
        target = QuestionnaireResponse.objects.select_for_update().get(pk=authorized.pk)
        if (
            target.state != "draft"
            or target.revision != expected_revision
            or answer_values(target)
        ):
            raise ValidationError(_("Carry-forward requires an empty current draft."))
        values = validate_answers(
            target.template.questions, answer_values(source), submitting=False
        )
        target.answers = {"values": values, "unverified": list(values)}
        target.revision += 1
        target.save(update_fields=("answers", "revision", "updated_at"))
        return target


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
