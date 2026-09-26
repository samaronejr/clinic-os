"""Actual clinical guard calls, including resolver and denial-helper boundaries."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.ehr import attachments, finalization, history, services
from apps.identity import physician_verification
from apps.intake import access
from apps.intake import demographics as intake_demographics
from apps.intake.models import PatientClinicEnrollment
from apps.prescription import services as prescription
from apps.retention import services as retention
from django.db import connection
from psycopg import sql

from identity.legacy_parity_support import ADMINS, LEGACY, MANAGERS, PHYSICIAN, Boundary

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld


def _enrollment(w: LegacyWorld) -> UUID:
    return PatientClinicEnrollment.objects.get(
        patient_id=w.appointment.patient_id, clinic_id=w.clinic
    ).pk


def _assignment(w: LegacyWorld, valid: bool) -> object:
    appointment = w.appointment
    original = appointment.practitioner_id
    if not valid:
        appointment.practitioner_id = uuid4()
    try:
        return services._assigned(appointment)
    finally:
        appointment.practitioner_id = original


def _sql(w: LegacyWorld, valid: bool, name: str) -> bool:
    # SQL identifiers are the closed test inventory, never request input.
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT clinic_app.{}(%s)").format(sql.Identifier(name)),
            [w.encounter_for(valid)],
        )
        row = cursor.fetchone()
    return bool(row == (True,))


def _projection(w: LegacyWorld, valid: bool, *, attachment: bool) -> bool:
    encounter = copy(w.version.document.encounter)
    if not valid:
        encounter.pk = uuid4()
    result = (
        attachments.attachment_context(encounter)
        if attachment
        else history._context(encounter, "problem")
    )
    return result.can_write


BOUNDARIES = (
    Boundary(
        "apps.ehr.attachments.attachment_context",
        "projection",
        PHYSICIAN,
        lambda w, ok: _projection(w, ok, attachment=True),
    ),
    Boundary(
        "apps.ehr.history._context",
        "projection",
        PHYSICIAN,
        lambda w, ok: _projection(w, ok, attachment=False),
    ),
    Boundary(
        "apps.ehr.services._appointment",
        "scope",
        LEGACY,
        lambda w, ok: services._appointment(w.clinic_for(ok), w.appointment.pk),
    ),
    Boundary("apps.ehr.services._assigned", "assignment", PHYSICIAN, _assignment),
    Boundary(
        "apps.ehr.services.publish_template",
        "service",
        ADMINS,
        lambda w, ok: services.publish_template(
            clinic_id=w.clinic_for(ok),
            key="synthetic-parity",
            title="Sintetico parity",
            prompts=dict.fromkeys(services.SOAP_FIELDS, "Sintetico"),
        ),
    ),
    Boundary(
        "apps.ehr.services.open_encounter",
        "service",
        PHYSICIAN,
        lambda w, ok: services.open_encounter(
            clinic_id=w.clinic_for(ok), appointment_id=w.appointment.pk
        ),
    ),
    Boundary(
        "apps.ehr.services.create_draft",
        "service",
        PHYSICIAN,
        lambda w, ok: services.create_draft(
            clinic_id=w.clinic_for(ok),
            encounter_id=w.encounter,
            template_id=w.template.pk,
        ),
    ),
    Boundary(
        "apps.ehr.services.view_version",
        "resolver",
        PHYSICIAN,
        lambda w, ok: services.view_version(
            clinic_id=w.clinic_for(ok), version_id=w.version.pk
        ),
    ),
    Boundary(
        "apps.ehr.services.record_clinical_note",
        "service",
        PHYSICIAN,
        lambda w, ok: services.record_clinical_note(
            clinic_id=w.clinic_for(ok),
            version_id=w.version.pk,
            expected_revision=w.version.revision,
            content=dict.fromkeys(services.SOAP_FIELDS, "Sintetico parity"),
        ),
    ),
    Boundary(
        "apps.ehr.finalization.finalize_version",
        "service",
        PHYSICIAN,
        lambda w, ok: finalization.finalize_version(
            clinic_id=w.clinic_for(ok),
            version_id=w.version.pk,
            expected_revision=w.version.revision,
            request=w.request,
        ),
    ),
    Boundary(
        "apps.ehr.finalization._author_version",
        "assignment",
        PHYSICIAN,
        lambda w, ok: finalization._author_version(w.clinic_for(ok), w.version.pk),
    ),
    Boundary(
        "apps.ehr.history.authorize_history",
        "resolver",
        PHYSICIAN,
        lambda w, ok: history.authorize_history(w.clinic_for(ok), w.encounter),
    ),
    Boundary(
        "apps.ehr.attachments.authorize_attachment_encounter",
        "resolver",
        PHYSICIAN,
        lambda w, ok: attachments.authorize_attachment_encounter(
            w.clinic_for(ok), w.encounter
        ),
    ),
    Boundary(
        "apps.ehr.history.save_history",
        "service",
        PHYSICIAN,
        lambda w, ok: history.save_history(
            clinic_id=w.clinic_for(ok),
            encounter_id=w.encounter,
            change=history.HistoryChange(
                kind="problem",
                expected_revision=0,
                state="none_documented",
                description="",
                status="",
                reason="Sintetico parity",
            ),
        ),
    ),
    Boundary(
        "apps.identity.physician_verification._assigned_encounter",
        "assignment",
        PHYSICIAN,
        lambda w, ok: physician_verification._assigned_encounter(
            w.request, w.clinic_for(ok), w.encounter
        ),
    ),
    Boundary(
        "apps.prescription.services.authorize_encounter",
        "assignment",
        PHYSICIAN,
        lambda w, ok: prescription.authorize_encounter(
            clinic_id=w.clinic_for(ok), encounter_id=w.encounter
        ),
    ),
    Boundary(
        "apps.prescription.services._scope",
        "binding",
        LEGACY,
        lambda w, ok: prescription._scope(
            w.version.document.encounter,
            w.appointment.patient_id if ok else uuid4(),
            w.graph.physician,
        ),
    ),
    Boundary(
        "apps.prescription.services._authorize_document_care",
        "resolver",
        PHYSICIAN,
        lambda w, ok: prescription._authorize_document_care(
            w.clinic_for(ok), w.encounter
        ),
    ),
    Boundary(
        "apps.intake.access.authorized_manager_clinic",
        "scope",
        MANAGERS,
        lambda w, ok: access.authorized_manager_clinic(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.intake.access.authorized_enrollment",
        "scope",
        MANAGERS,
        lambda w, ok: access.authorized_enrollment(w.clinic_for(ok), _enrollment(w)),
    ),
    Boundary(
        "apps.intake.access.authorized_enrollment_for",
        "scope",
        # demographics.read is granted to receptionist and physician bundles
        # plus the clinic admin/manager bundles.
        ("physician", "receptionist", "clinic_admin"),
        lambda w, ok: access.authorized_enrollment_for(
            w.clinic_for(ok), _enrollment(w), "demographics.read"
        ),
    ),
    Boundary(
        "apps.intake.demographics.search_patient_identifiers",
        "scope",
        ("physician", "receptionist", "clinic_admin"),
        lambda w, ok: intake_demographics.search_patient_identifiers(
            clinic_id=w.clinic_for(ok),
            kind="cpf",
            value="52998224725",
        ),
    ),
    Boundary(
        "apps.intake.demographics.set_intake_policy",
        "roles",
        # Intake policy is clinic configuration: clinic_admin holds
        # configuration.clinic, owner holds configuration.organization.
        ADMINS,
        lambda w, ok: intake_demographics.set_intake_policy(
            clinic_id=w.clinic_for(ok), required_fields=("legal_name",)
        ),
    ),
    Boundary(
        "apps.retention.services._require_manager",
        "roles",
        ADMINS,
        lambda w, ok: retention._require_manager(w.clinic_for(ok), w.encounter),
    ),
    Boundary(
        "apps.retention.services._record_scope",
        "resolver",
        ADMINS,
        lambda w, ok: retention._record_scope("ehr.encounter", w.encounter_for(ok)),
    ),
    Boundary(
        "clinic_app.ehr_assigned",
        "sql",
        PHYSICIAN,
        lambda w, ok: _sql(w, ok, "ehr_assigned"),
    ),
    Boundary(
        "clinic_app.ehr_care", "sql", PHYSICIAN, lambda w, ok: _sql(w, ok, "ehr_care")
    ),
    Boundary(
        "clinic_app.ehr_history_care",
        "sql",
        PHYSICIAN,
        lambda w, ok: _sql(w, ok, "ehr_history_care"),
    ),
)
