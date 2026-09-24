"""Clinical authority derives only from the current transaction and canonical roles."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NoReturn

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.canonical import AuditEventInput
from apps.audit.services import record_event, record_phase1_event
from apps.ehr.models import (
    ClinicalDocument,
    ClinicalDocumentVersion,
    Encounter,
    EncounterIntakeReference,
    SpecialtyTemplate,
)
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.overlay_content import validate_overlay_text
from apps.intake.models import (
    PatientClinicEnrollment,
    QuestionnaireEvent,
    QuestionnaireResponse,
)
from apps.scheduling.models import Appointment

if TYPE_CHECKING:
    from uuid import UUID

SOAP_FIELDS = ("subjective", "objective", "assessment", "plan")
MAX_CONTENT = 20000
MAX_PROMPT = 1000
MAX_TITLE = 160


class ClinicalAccessDeniedError(Exception):
    """Non-enumerating clinical boundary failure."""


class ClinicalConflictError(Exception):
    """A fixed lifecycle conflict; no caller content appears in its message."""

    def __init__(self, reason_code: str) -> None:
        """Retain the machine-consumed conflict reason."""
        self.reason_code = reason_code
        super().__init__(reason_code)


def record_denial(clinic_id: UUID, record_id: UUID, reason: str) -> None:
    """Append the fixed metadata-only denial event for one clinical record."""
    record_event(
        AuditEventInput(
            event_type="ehr.access.denied",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type="ehr.record",
            affected_record_id=str(record_id),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(clinic_id), "reason_code": reason},
    )


def _denied(clinic_id: UUID, record_id: UUID, reason: str) -> NoReturn:
    record_denial(clinic_id, record_id, reason)
    raise ClinicalAccessDeniedError


def _appointment(clinic_id: UUID, appointment_id: UUID) -> Appointment:
    appointment = Appointment.objects.filter(
        pk=appointment_id, clinic_id=clinic_id
    ).first()
    if appointment is None:
        raise ClinicalAccessDeniedError
    # A different clinic must be indistinguishable from an unknown record.
    if not UserClinicRole.objects.filter(
        clinic_id=clinic_id, user_id=current_actor_id()
    ).exists():
        raise ClinicalAccessDeniedError
    return appointment


def _assigned(appointment: Appointment) -> UUID:
    try:
        actor = require_current_actor_clinic_roles(
            appointment.clinic_id, (UserClinicRole.Role.PHYSICIAN,)
        )
    except CurrentActorError:
        _denied(appointment.clinic_id, appointment.pk, "role_denied")
    if actor != appointment.practitioner_id:
        _denied(appointment.clinic_id, appointment.pk, "not_assigned")
    return actor


def publish_template(
    *, clinic_id: UUID, key: str, title: str, prompts: dict[str, str]
) -> SpecialtyTemplate:
    """Publish immutable data-only SOAP prompts with serialized version allocation."""
    require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN)
    )
    if (
        not re.fullmatch(r"[a-z0-9_-]{1,64}", key)
        or not title.strip()
        or len(title) > MAX_TITLE
        or set(prompts) != set(SOAP_FIELDS)
        or any(not isinstance(v, str) or len(v) > MAX_PROMPT for v in prompts.values())
    ):
        msg = "Modelo de especialidade inválido."
        raise ValidationError(msg)
    validate_overlay_text(title)
    for prompt in prompts.values():
        validate_overlay_text(prompt)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"ehr-template:{clinic_id}:{key}"],
        )
        clinic = Clinic.objects.get(pk=clinic_id)
        previous = (
            SpecialtyTemplate.objects.filter(clinic_id=clinic_id, key=key)
            .order_by("-version")
            .first()
        )
        result = SpecialtyTemplate.objects.create(
            organization_id=clinic.organization_id,
            clinic=clinic,
            key=key,
            title=title,
            prompts=prompts,
            version=previous.version + 1 if previous else 1,
        )
        record_phase1_event(
            "ehr.template.published", clinic_id=clinic_id, affected_record_id=result.pk
        )
        return result


def open_encounter(*, clinic_id: UUID, appointment_id: UUID) -> Encounter:
    """Serialize starts; cancellation never removes an existing record."""
    appointment = _appointment(clinic_id, appointment_id)
    actor = _assigned(appointment)
    with transaction.atomic():
        appointment = Appointment.objects.select_for_update().get(pk=appointment.pk)
        existing = Encounter.objects.filter(appointment=appointment).first()
        if existing is not None:
            return existing
        if appointment.status != Appointment.Status.SCHEDULED:
            msg = "precondition_failed"
            raise ClinicalConflictError(msg)
        if not PatientClinicEnrollment.objects.filter(
            clinic_id=clinic_id,
            patient_id=appointment.patient_id,
            organization_id=appointment.organization_id,
        ).exists():
            raise ClinicalAccessDeniedError
        encounter = Encounter.objects.create(
            organization_id=appointment.organization_id,
            clinic_id=clinic_id,
            appointment=appointment,
            patient_id=appointment.patient_id,
            physician_id=actor,
        )
        responses = QuestionnaireResponse.objects.filter(
            clinic_id=clinic_id,
            patient_id=appointment.patient_id,
            state="submitted",
        ).filter(Q(appointment_id=appointment.pk) | Q(appointment_id__isnull=True))
        for response in responses:
            submission = QuestionnaireEvent.objects.get(
                response=response, revision=response.revision, action="submitted"
            )
            EncounterIntakeReference.objects.create(
                organization_id=encounter.organization_id,
                encounter=encounter,
                submission=submission,
            )
        record_phase1_event(
            "ehr.encounter.opened", clinic_id=clinic_id, affected_record_id=encounter.pk
        )
        return encounter


def create_draft(
    *, clinic_id: UUID, encounter_id: UUID, template_id: UUID
) -> ClinicalDocumentVersion:
    """Create or resume the exact first SOAP version without replacing its template."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    actor = _assigned(_appointment(clinic_id, encounter.appointment_id))
    with transaction.atomic():
        encounter = Encounter.objects.select_for_update().get(pk=encounter.pk)
        existing = (
            ClinicalDocumentVersion.objects.filter(document__encounter=encounter)
            .exclude(state="discarded")
            .order_by("-version")
            .first()
        )
        if existing is not None:
            return view_version(clinic_id=clinic_id, version_id=existing.pk)
        if encounter.state != Encounter.State.OPEN:
            msg = "encounter_closed"
            raise ClinicalConflictError(msg)
        template = SpecialtyTemplate.objects.filter(
            pk=template_id, clinic_id=clinic_id
        ).first()
        if template is None:
            raise ClinicalAccessDeniedError
        document = ClinicalDocument.objects.filter(encounter=encounter).first()
        if document is None:
            document = ClinicalDocument.objects.create(
                organization_id=encounter.organization_id, encounter=encounter
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT clinic_app.ehr_next_version(%s)", [document.pk])
            next_version = int(cursor.fetchone()[0])
        version = ClinicalDocumentVersion.objects.create(
            organization_id=encounter.organization_id,
            document=document,
            template=template,
            author_id=actor,
            version=next_version,
        )
        record_phase1_event(
            "ehr.document.draft_created",
            clinic_id=clinic_id,
            affected_record_id=version.pk,
        )
        return version


def view_version(*, clinic_id: UUID, version_id: UUID) -> ClinicalDocumentVersion:
    """Read only versions admitted by both the clinical predicate and FORCE RLS."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ehr_version_scope(%s, %s)", [clinic_id, version_id]
        )
        scope = cursor.fetchone()
    if scope is None or scope[0] is None:
        raise ClinicalAccessDeniedError
    try:
        require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    except CurrentActorError:
        _denied(clinic_id, version_id, "role_denied")
    version = (
        ClinicalDocumentVersion.objects.filter(
            pk=version_id, document__encounter__clinic_id=clinic_id
        )
        .select_related("template", "document__encounter")
        .first()
    )
    if version is None:
        _denied(clinic_id, version_id, "no_care_relationship")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ehr_assigned(%s), clinic_app.ehr_care(%s)",
            [version.document.encounter_id, version.document.encounter_id],
        )
        predicates = cursor.fetchone()
    assigned, care = predicates or (False, False)
    if (
        version.state == "discarded"
        or (
            version.state == "draft"
            and (not assigned or version.author_id != current_actor_id())
        )
        or (not assigned and not care)
    ):
        _denied(clinic_id, version.pk, "no_care_relationship")
    record_phase1_event(
        "ehr.record.viewed", clinic_id=clinic_id, affected_record_id=version.pk
    )
    return version


def record_clinical_note(
    *,
    clinic_id: UUID,
    version_id: UUID,
    expected_revision: int,
    content: dict[str, str],
) -> ClinicalDocumentVersion:
    """Save explicitly; stale editors and failed transactions retain prior data."""
    authorized = view_version(clinic_id=clinic_id, version_id=version_id)
    actor = _assigned(
        _appointment(clinic_id, authorized.document.encounter.appointment_id)
    )
    if authorized.author_id != actor:
        _denied(clinic_id, version_id, "not_assigned")
    if set(content) != set(SOAP_FIELDS) or any(
        not isinstance(v, str) or len(v) > MAX_CONTENT for v in content.values()
    ):
        msg = "Preencha somente os campos SOAP, com até 20.000 caracteres cada."
        raise ValidationError(msg)
    with transaction.atomic():
        # A finalized/superseded row is readable but outside the UPDATE
        # policy, so select_for_update filters it out; that non-draft path
        # is the contract's precondition_failed conflict, never a 500.
        version = (
            ClinicalDocumentVersion.objects.select_for_update()
            .filter(pk=version_id)
            .first()
        )
        if version is None or version.state != "draft":
            msg = "precondition_failed"
            raise ClinicalConflictError(msg)
        if version.revision != expected_revision:
            msg = "stale_revision"
            raise ClinicalConflictError(msg)
        stored_sha256 = version.content_sha256
        version.set_soap(content)
        if version.content_sha256 == stored_sha256:
            # An unchanged save is a no-op: re-encrypting the same plaintext
            # would change the envelope bytes while the digest stays equal,
            # which the binding guard rejects as an inconsistent transition.
            return version
        version.revision += 1
        version.save(
            update_fields=("content", "content_sha256", "revision", "updated_at")
        )
        record_phase1_event(
            "ehr.document.saved", clinic_id=clinic_id, affected_record_id=version.pk
        )
        return version
