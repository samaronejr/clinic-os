"""Real-schema populated EHR protected-field migration proof.

The surrogate probe and the intake/prescription real-schema test cannot
reproduce the EHR digest contract: the migrated ``content_sha256`` must equal
the RFC 8785 canonical digest the runtime recomputes when an amendment draft
carries the base version's content. This test migrates a scratch database to
the exact pre-migration state, seeds a finalized SOAP note with accented
Portuguese content through the real historical guards, applies the actual
``ehr.0009`` migration, then amends, edits and finalizes the note through the
real service boundary as ``clinic_app``.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
import rfc8785
from apps.ehr.finalization import amend_document, content_digest, finalize_version
from apps.ehr.services import record_clinical_note, view_version
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import reveal
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.backends.postgresql.base import DatabaseWrapper

from auth.stepup_test_support import verified_request
from patient_service_support import runtime_role
from patients.test_intake_migrations import (
    LEGACY_CLINIC,
    LEGACY_ENCOUNTER,
    LEGACY_ORG,
    LEGACY_PHYSICIAN,
    _default_connection,
    _scratch_database,
    _scratch_tenant_key,
    _seed_legacy_clinic,
)

LEGACY_TEMPLATE_EHR = UUID(int=3018)
LEGACY_DOCUMENT_EHR = UUID(int=3019)
LEGACY_VERSION_DISCARDED = UUID(int=3020)
LEGACY_VERSION_FINAL = UUID(int=3021)
LEGACY_SOAP = {
    "subjective": "Avaliação sintética",
    "objective": "Exame clínico",
    "assessment": "Hipótese sintética",
    "plan": "Revisão",
}
LEGACY_SOAP_DISCARDED = {
    "subjective": "Rascunho descartado",
    "objective": "Observação prévia",
    "assessment": "Sem conclusão",
    "plan": "Reagendar",
}
AMENDED_SOAP = {
    "subjective": "Avaliação retificada",
    "objective": "Exame clínico",
    "assessment": "Diagnóstico confirmado",
    "plan": "Revisão em 30 dias",
}


def _seed_legacy_ehr_note(wrapper: DatabaseWrapper) -> None:
    """Create one finalized accented SOAP note through the historical guards.

    The historical models at ``prescription.0009`` state carry the plaintext
    SOAP columns; the real ``ehr.0008`` binding guard admits only the
    draft-save, finalize and discard transitions used here. A discarded
    draft is included so the archived-content encoder is exercised too.
    """
    alias = wrapper.alias
    executor = MigrationExecutor(wrapper)
    state = executor.loader.project_state([("prescription", "0009_protected_fields")])
    historical = state.apps
    template_model = historical.get_model("ehr", "SpecialtyTemplate")
    document_model = historical.get_model("ehr", "ClinicalDocument")
    version_model = historical.get_model("ehr", "ClinicalDocumentVersion")

    with transaction.atomic(using=alias), wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(LEGACY_ORG)],
        )
        template = template_model.objects.using(alias).create(
            id=LEGACY_TEMPLATE_EHR,
            organization_id=LEGACY_ORG,
            clinic_id=LEGACY_CLINIC,
            key="gatelegacy",
            version=1,
            title="Legacy SOAP",
            prompts={key: key for key in LEGACY_SOAP},
        )
        document = document_model.objects.using(alias).create(
            id=LEGACY_DOCUMENT_EHR,
            organization_id=LEGACY_ORG,
            encounter_id=LEGACY_ENCOUNTER,
        )
        discarded = version_model.objects.using(alias).create(
            id=LEGACY_VERSION_DISCARDED,
            organization_id=LEGACY_ORG,
            document=document,
            template=template,
            author_id=LEGACY_PHYSICIAN,
            version=1,
        )
        discarded.subjective = LEGACY_SOAP_DISCARDED["subjective"]
        discarded.objective = LEGACY_SOAP_DISCARDED["objective"]
        discarded.assessment = LEGACY_SOAP_DISCARDED["assessment"]
        discarded.plan = LEGACY_SOAP_DISCARDED["plan"]
        discarded.revision = 2
        discarded.save(using=alias)
        discarded.state = "discarded"
        discarded.save(using=alias)

        version = version_model.objects.using(alias).create(
            id=LEGACY_VERSION_FINAL,
            organization_id=LEGACY_ORG,
            document=document,
            template=template,
            author_id=LEGACY_PHYSICIAN,
            version=2,
        )
        version.subjective = LEGACY_SOAP["subjective"]
        version.objective = LEGACY_SOAP["objective"]
        version.assessment = LEGACY_SOAP["assessment"]
        version.plan = LEGACY_SOAP["plan"]
        version.revision = 2
        version.save(using=alias)
        # The finalized digest is the immutable document digest the runtime
        # computes: RFC 8785 over the template binding plus the SOAP fields.
        version.state = "finalized"
        version.finalized_at = timezone.now()
        version.content_digest = hashlib.sha256(
            rfc8785.dumps(
                {
                    "v": "ehr-document-v1",
                    "template_id": str(template.pk),
                    "template_version": template.version,
                    **LEGACY_SOAP,
                }
            )
        ).hexdigest()
        version.save(using=alias)


@pytest.mark.django_db(transaction=True)
def test_migrated_accented_soap_note_amends_and_finalizes(
    superuser_database_url: str,
) -> None:
    """A migrated accented finalized note stays amendable as clinic_app.

    The migrated ``content_sha256`` must equal the runtime RFC 8785 digest or
    the binding trigger rejects the amendment draft as invalid; the archived
    discarded body must decrypt to the same canonical encoding; and the
    original finalized content/digest must survive amendment untouched.
    """
    with _scratch_database(superuser_database_url) as wrapper:
        MigrationExecutor(wrapper).migrate([("prescription", "0009_protected_fields")])
        _seed_legacy_clinic(wrapper)
        _scratch_tenant_key(wrapper, LEGACY_ORG)
        _seed_legacy_ehr_note(wrapper)

        MigrationExecutor(wrapper).migrate(
            [
                ("ehr", "0009_protected_fields"),
                ("intake", "0011_protected_fields"),
            ]
        )

        with _default_connection(wrapper):
            request = verified_request(LEGACY_PHYSICIAN)
            with runtime_role(), tenant_context(LEGACY_PHYSICIAN, LEGACY_ORG):
                restored = view_version(
                    clinic_id=LEGACY_CLINIC, version_id=LEGACY_VERSION_FINAL
                )
                assert restored.state == "finalized"
                assert restored.soap == LEGACY_SOAP
                # The stored digest is exactly what set_soap recomputes for
                # the same content: the amendment guard's equality proof.
                assert (
                    restored.content_sha256
                    == hashlib.sha256(rfc8785.dumps(LEGACY_SOAP)).hexdigest()
                )
                original_digest = restored.content_digest
                assert original_digest == content_digest(restored)

                amendment = amend_document(
                    clinic_id=LEGACY_CLINIC,
                    version_id=restored.pk,
                    reason="Correção sintética",
                )
                assert amendment.state == "draft"
                assert amendment.amendment_of_id == restored.pk
                assert amendment.soap == LEGACY_SOAP
                record_clinical_note(
                    clinic_id=LEGACY_CLINIC,
                    version_id=amendment.pk,
                    expected_revision=1,
                    content=AMENDED_SOAP,
                )
                finalized = finalize_version(
                    clinic_id=LEGACY_CLINIC,
                    version_id=amendment.pk,
                    expected_revision=2,
                    request=request,
                )
                assert finalized.state == "finalized"
                assert finalized.soap == AMENDED_SOAP
                assert finalized.content_digest == content_digest(finalized)

                base = view_version(
                    clinic_id=LEGACY_CLINIC, version_id=LEGACY_VERSION_FINAL
                )
                assert base.state == "superseded"
                assert base.soap == LEGACY_SOAP
                assert base.content_digest == original_digest
                assert base.content_sha256 == restored.content_sha256

            # The discarded draft's archived body kept the same canonical
            # encoding: the owner reads the archive row, the runtime role
            # decrypts it, and the recorded digest matches the runtime codec.
            with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    [str(LEGACY_ORG)],
                )
                cursor.execute(
                    "SELECT content, content_sha256 "
                    "FROM clinic_app.ehr_discarded_content WHERE version_id = %s",
                    [str(LEGACY_VERSION_DISCARDED)],
                )
                archived = cursor.fetchone()
            assert archived is not None
            archived_envelope, archived_sha256 = archived
            assert archived_envelope is not None
            assert (
                archived_sha256
                == hashlib.sha256(rfc8785.dumps(LEGACY_SOAP_DISCARDED)).hexdigest()
            )
            with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    [str(LEGACY_ORG)],
                )
                decrypted = reveal(
                    purpose="ehr.clinicaldocumentversion.content",
                    envelope=bytes(archived_envelope),
                )
            assert json.loads(decrypted) == LEGACY_SOAP_DISCARDED
