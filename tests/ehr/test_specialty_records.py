"""Encrypted specialty facts preserve care scope and real attachment bindings."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.attachments import scan_attachment
from apps.ehr.models import ClinicalDocument, ClinicalDocumentVersion, Encounter
from apps.ehr.pack_models import SpecialtyEntry
from apps.ehr.packs import install_pack, uninstall_pack
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.ehr.specialty_records import (
    ImmunizationInput,
    ProcedureInput,
    record_immunization,
    record_procedure,
)
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction

from ehr.longitudinal_support import chart_world
from ehr.test_addenda import signed_in
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_attachments import PNG_BYTES, attachment_root, upload

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

__all__ = ("attachment_root",)
pytestmark = pytest.mark.django_db(transaction=True)
SENTINEL = "SINTETICO-SENTINELA-30"


def test_immunization_is_enveloped_unconfirmed_and_retained(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    encounter = world.version.document.encounter
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        install_pack(clinic_id=rbac_graph.clinic_a, pack_key="pediatrics", version=1)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = record_immunization(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            record=ImmunizationInput(
                SENTINEL, date(2020, 1, 2), "Recorded dose", "reported_by_patient"
            ),
        )
        assert row.details["origin"] == "reported_by_patient"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT details FROM clinic_app.ehr_specialtyentry WHERE id=%s",
                [row.pk],
            )
            assert SENTINEL.encode() not in bytes(cursor.fetchone()[0])
        with pytest.raises(DatabaseError), transaction.atomic():
            SpecialtyEntry.objects.filter(pk=row.pk).delete()
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        uninstall_pack(clinic_id=rbac_graph.clinic_a, pack_key="pediatrics", version=1)
        assert not SpecialtyEntry.objects.filter(pk=row.pk).exists()
    with as_actor(rbac_graph, rbac_graph.physician):
        assert SpecialtyEntry.objects.get(pk=row.pk).details["vaccine"] == SENTINEL
        with pytest.raises(ClinicalConflictError, match="pack_unavailable"):
            record_immunization(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                record=ImmunizationInput(
                    "Synthetic", date(2020, 1, 2), "Dose", "observed"
                ),
            )
    with owner_context(rbac_graph.organization_a):
        events = AuditEvent.objects.filter(affected_record_id=str(row.pk))
        assert SENTINEL not in json.dumps(
            list(events.values_list("payload", flat=True))
        )


def test_foreign_and_unknown_specialty_encounters_have_identical_denials(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        for clinic, encounter in (
            (rbac_graph.clinic_c, world.version.document.encounter_id),
            (rbac_graph.clinic_a, uuid4()),
        ):
            with pytest.raises(ClinicalAccessDeniedError):
                record_immunization(
                    clinic_id=clinic,
                    encounter_id=encounter,
                    record=ImmunizationInput(
                        "Synthetic", date(2020, 1, 2), "Dose", "observed"
                    ),
                )
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true), "
            "set_config('app.current_tenant',%s,true)",
            [str(rbac_graph.physician), str(rbac_graph.organization_b)],
        )
        assert SpecialtyEntry.objects.count() == 0


def test_procedure_links_only_scanned_same_encounter_sources(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    world = chart_world(rbac_graph)
    encounter = world.version.document.encounter
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        install_pack(clinic_id=rbac_graph.clinic_a, pack_key="procedures", version=1)
    with as_actor(rbac_graph, rbac_graph.physician):
        consent = upload(rbac_graph, encounter)
        with pytest.raises(ClinicalAccessDeniedError):
            record_procedure(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                record=ProcedureInput(SENTINEL, consent.pk),
            )
        scan_attachment(clinic_id=rbac_graph.clinic_a, attachment_id=consent.pk)
        image = upload(rbac_graph, encounter, PNG_BYTES, "image/png")
        scan_attachment(clinic_id=rbac_graph.clinic_a, attachment_id=image.pk)
        row = record_procedure(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            record=ProcedureInput(SENTINEL, consent.pk, (image.pk,)),
        )
        assert row.consent_document_id == consent.pk
        assert row.images == [str(image.pk)]
        assert row.details == {"plan": SENTINEL}
        with pytest.raises(ClinicalAccessDeniedError):
            record_procedure(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                record=ProcedureInput(SENTINEL, uuid4()),
            )
        with pytest.raises(ValidationError):
            record_procedure(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                record=ProcedureInput(SENTINEL, consent.pk, (image.pk, image.pk)),
            )
    with signed_in(rbac_graph.physician) as client:
        page = client.post(
            f"/ehr/clinics/{rbac_graph.clinic_a}/patient-workspace/",
            {"action": "open", "enrollment_id": world.enrollment, "tab": "specialty"},
        )
        assert page.status_code == 200
        assert page.content.count(b'name="attachment_id"') == 2
        for attachment in (consent, image):
            downloaded = client.post(
                f"/ehr/clinics/{rbac_graph.clinic_a}/attachments/",
                {
                    "action": "download",
                    "encounter_id": encounter.pk,
                    "attachment_id": attachment.pk,
                },
            )
            assert downloaded.status_code == 200
            assert downloaded["Content-Disposition"].startswith("attachment;")


def test_direct_psychiatry_version_cannot_omit_restriction(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        pack = install_pack(
            clinic_id=rbac_graph.clinic_a, pack_key="psychiatry", version=1
        )
    # A second kind is not admitted by the existing document guard. Instead,
    # a fresh document is scoped to another retained open unscheduled encounter.
    with owner_context(rbac_graph.organization_a):
        encounter = Encounter.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_id=world.patient,
            physician_id=rbac_graph.physician,
            unscheduled_reason="documentation_only",
        )
    with as_actor(rbac_graph, rbac_graph.physician):
        document = ClinicalDocument.objects.create(
            organization_id=rbac_graph.organization_a, encounter=encounter
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.create(
                organization_id=rbac_graph.organization_a,
                document=document,
                template_id=pack.template_id,
                author_id=rbac_graph.physician,
                version=1,
            )
