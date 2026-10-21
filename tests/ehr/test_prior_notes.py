"""Prior-note reuse cannot bypass CAS, source scope, restriction or explicit review."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.finalization import content_digest, finalize_version
from apps.ehr.models import ClinicalDocumentVersion, CopiedNoteBlock, Encounter
from apps.ehr.note_access import restrict_note
from apps.ehr.prior_notes import CopyNoteCommand, reuse_prior_note, review_copied_block
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    create_draft,
    record_clinical_note,
)
from apps.identity.models import CareTeamMembership, RoleGrant
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.tenancy import fields
from apps.tenancy.envelope import reveal as reveal_envelope
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext

from auth.stepup_test_support import verified_request
from ehr.longitudinal_support import chart_world, finalized, second_draft
from ehr.test_autosave import SECTIONS, as_actor
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_foreign_patient_source_is_refused_before_decryption(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = chart_world(rbac_graph)
    source = finalized(rbac_graph, world.version)
    with owner_context(rbac_graph.organization_a), connection.cursor() as cursor:
        patient = Patient.objects.create(
            organization_id=rbac_graph.organization_a,
            full_name="Synthetic other patient",
            birth_date=date(1990, 1, 1),
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        )
        CareTeamMembership.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_enrollment=enrollment,
            user_id=rbac_graph.physician,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
        )
        encounter = Encounter.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient=patient,
            physician_id=rbac_graph.physician,
            unscheduled_reason="documentation_only",
        )
        cursor.execute(
            "SELECT content FROM clinic_app.ehr_clinicaldocumentversion WHERE id=%s",
            [source.pk],
        )
        row = cursor.fetchone()
        assert row is not None
        source_envelope = bytes(row[0])
    with as_actor(rbac_graph, rbac_graph.physician):
        target = create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=world.version.template_id,
        )
    source_reads: list[str] = []

    def reveal(*, purpose: str, envelope: bytes) -> bytes:
        if envelope == source_envelope:
            source_reads.append(purpose)
        return reveal_envelope(purpose=purpose, envelope=envelope)

    monkeypatch.setattr(fields, "reveal", reveal)
    with (
        as_actor(rbac_graph, rbac_graph.physician),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        reuse_prior_note(
            clinic_id=rbac_graph.clinic_a,
            target_version_id=target.pk,
            source_version_id=source.pk,
            command=CopyNoteCommand(
                section="subjective", expected_revision=1, command_id=uuid4()
            ),
        )
    assert source_reads == []
    with owner_context(rbac_graph.organization_a):
        assert not CopiedNoteBlock.objects.exists()
        assert ClinicalDocumentVersion.objects.get(pk=target.pk).revision == 1


def test_copy_requires_read_permission_independently_of_write(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    source = finalized(rbac_graph, world.version)
    target = second_draft(rbac_graph, world, rbac_graph.physician)
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="physician",
            permission="clinical.read",
            bundle_version=1,
            effect="remove",
            valid_from=timezone.now(),
        )
    with (
        as_actor(rbac_graph, rbac_graph.physician),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        reuse_prior_note(
            clinic_id=rbac_graph.clinic_a,
            target_version_id=target.pk,
            source_version_id=source.pk,
            command=CopyNoteCommand(
                section="subjective", expected_revision=1, command_id=uuid4()
            ),
        )
    with owner_context(rbac_graph.organization_a):
        assert ClinicalDocumentVersion.objects.get(pk=target.pk).revision == 1
        assert CopiedNoteBlock.objects.count() == 0


def test_copy_requires_review_at_the_current_revision(rbac_graph: RbacGraph) -> None:
    world = chart_world(rbac_graph)
    source = finalized(rbac_graph, world.version)
    target = second_draft(rbac_graph, world, rbac_graph.physician)
    request = verified_request(rbac_graph.physician)
    with as_actor(rbac_graph, rbac_graph.physician):
        target = record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=target.pk,
            expected_revision=1,
            content=SECTIONS,
        )
        command = uuid4()
        block = reuse_prior_note(
            clinic_id=rbac_graph.clinic_a,
            target_version_id=target.pk,
            source_version_id=source.pk,
            command=CopyNoteCommand(
                section="subjective", expected_revision=2, command_id=command
            ),
        )
        saved = ClinicalDocumentVersion.objects.get(pk=target.pk)
        assert saved.revision == 3
        assert SECTIONS["subjective"] in saved.subjective
        assert gettext("Copied from %(date)s").split("%(date)s")[0] in saved.subjective
        assert block.reviewed_at is None
        assert (
            reuse_prior_note(
                clinic_id=rbac_graph.clinic_a,
                target_version_id=target.pk,
                source_version_id=source.pk,
                command=CopyNoteCommand(
                    section="subjective", expected_revision=2, command_id=command
                ),
            ).pk
            == block.pk
        )
        with pytest.raises(ClinicalConflictError, match="copied_blocks_unreviewed"):
            finalize_version(
                clinic_id=rbac_graph.clinic_a,
                version_id=target.pk,
                expected_revision=3,
                request=request,
            )
        with (
            pytest.raises(DatabaseError, match="copied_blocks_unreviewed"),
            transaction.atomic(),
        ):
            ClinicalDocumentVersion.objects.filter(pk=target.pk).update(
                state="finalized",
                finalized_at=timezone.now(),
                content_digest=content_digest(saved),
            )
        review_copied_block(
            clinic_id=rbac_graph.clinic_a, block_id=block.pk, expected_revision=3
        )
        changed = dict(saved.soap)
        changed["plan"] = "Plano sintético revisado"
        updated = record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=target.pk,
            expected_revision=3,
            content=changed,
        )
        with pytest.raises(ClinicalConflictError, match="copied_blocks_unreviewed"):
            finalize_version(
                clinic_id=rbac_graph.clinic_a,
                version_id=target.pk,
                expected_revision=updated.revision,
                request=request,
            )
        review_copied_block(
            clinic_id=rbac_graph.clinic_a,
            block_id=block.pk,
            expected_revision=updated.revision,
        )
        result = finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=target.pk,
            expected_revision=updated.revision,
            request=request,
        )
        assert result.state == "finalized"


def test_restricted_copy_never_widens_the_source_audience(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    source = finalized(rbac_graph, world.version)
    target = second_draft(rbac_graph, world, rbac_graph.physician)
    with as_actor(rbac_graph, rbac_graph.physician):
        restrict_note(clinic_id=rbac_graph.clinic_a, version_id=source.pk)
        with pytest.raises(ClinicalConflictError, match="restricted_source"):
            reuse_prior_note(
                clinic_id=rbac_graph.clinic_a,
                target_version_id=target.pk,
                source_version_id=source.pk,
                command=CopyNoteCommand(
                    section="subjective", expected_revision=1, command_id=uuid4()
                ),
            )
        assert ClinicalDocumentVersion.objects.get(pk=target.pk).revision == 1
        assert CopiedNoteBlock.objects.count() == 0
        restrict_note(clinic_id=rbac_graph.clinic_a, version_id=target.pk)
        copied = reuse_prior_note(
            clinic_id=rbac_graph.clinic_a,
            target_version_id=target.pk,
            source_version_id=source.pk,
            command=CopyNoteCommand(
                section="subjective", expected_revision=1, command_id=uuid4()
            ),
        )
        assert copied.reviewed_at is None
    with owner_context(rbac_graph.organization_a):
        assert ClinicalDocumentVersion.objects.get(pk=source.pk).state == "finalized"
