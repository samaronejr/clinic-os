"""Drive v5 export → governed import, with independent PostgreSQL readback."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
import rfc8785
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.ehr.attachments import AttachmentInput, scan_attachment, upload_attachment
from apps.ehr.finalization import finalize_version
from apps.ehr.longitudinal_models import Observation
from apps.ehr.models import ClinicalAttachment, ClinicalDocumentVersion, Encounter
from apps.ehr.observation_services import ObservationInput, record_observation
from apps.ehr.services import (
    create_draft,
    open_encounter,
    publish_template,
    record_clinical_note,
)
from apps.identity.models import (
    CareTeamMembership,
    Clinic,
    ProfessionalRegistration,
    UserClinicRole,
)
from apps.intake.models import (
    DemographicsCorrection,
    Patient,
    PatientClinicEnrollment,
    PatientDemographics,
    PatientIdentifier,
)
from apps.intake.services import register_patient
from apps.interop import services, tasks
from apps.interop.access import Code, ImportConflictError, ImportInputError
from apps.interop.export_import import IMPORTABLE_DATASETS
from apps.interop.models import (
    ImportBatch,
    ImportedRecord,
    ImportFile,
    ImportJob,
    ImportMapping,
    ImportTransition,
)
from apps.retention.services import verify_export_package
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context
from django.utils import timezone

from auth.stepup_test_support import create_role_actor, verified_request
from identity.permission_support import owner_context
from interop.support import (
    approve,
    cpf,
    import_world,
    operating,
    org_admin,
    part,
    run_batches,
)
from interop.test_org_export import _archive, _build, _receipt, _request, dispatched
from patient_service_support import runtime_role
from renewal.test_attachments import PDF_BYTES, PNG_BYTES, attachment_root
from renewal.test_encounters import CONTENT
from scheduling.appointment_service_support import (
    AppointmentSetup,
    arrived_synthetic_appointment,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.interop.webhooks import JsonValue
    from apps.scheduling.models import Appointment

    from interop.support import ImportWorld
    from rbac_fixtures import RbacGraph

__all__ = ("attachment_root", "dispatched")
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("dispatched"),
]

MEASURED = datetime(2022, 2, 3, 4, 5, 6, 789123, tzinfo=UTC)
EXPECTED_COUNTS = {
    "intake_patient": 2,
    "intake_patientdemographics": 2,
    "intake_patientidentifier": 2,
    "ehr_clinicaldocumentversion": 1,
    "ehr_observation": 2,
    "ehr_clinicalattachment": 2,
}


def _care_for(graph: RbacGraph, enrollments: list[UUID]) -> None:
    now = timezone.now()
    with owner_context(graph.organization_a):
        clinic = Clinic.objects.get(pk=graph.clinic_a)
        ProfessionalRegistration.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.physician,
            role="physician",
            council="CRM",
            number="SINTETICO-F6",
            jurisdiction=clinic.crm_uf,
            specialty="Sintetico",
            status="regular",
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )
        for enrollment_id in enrollments:
            CareTeamMembership.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_enrollment_id=enrollment_id,
                user_id=graph.physician,
                role="physician",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )


def _seed_identities(graph: RbacGraph) -> tuple[UUID, UUID]:
    registrar = create_role_actor(graph, UserClinicRole.Role.CLINIC_ADMIN)
    with runtime_role(), tenant_context(registrar.pk, graph.organization_a):
        first = register_patient(
            clinic_id=graph.clinic_a,
            idempotency_key=uuid4(),
            legal_name="Paciente Sintética F6 Alfa",
            social_name="Sintética Alfa",
            birth_date=date(1994, 5, 18),
            changes={"sex_at_birth": "female"},
            identifier_kind="cpf",
            identifier_value=cpf(66),
        )
        second = register_patient(
            clinic_id=graph.clinic_a,
            idempotency_key=uuid4(),
            legal_name="",
            social_name="Paciente Sintético F6 Beta",
            birth_date=None,
            changes={"sex_at_birth": "not_informed"},
            identifier_kind="cns",
            identifier_value="100000000000066",
        )
    return first.registration.enrollment.pk, second.registration.enrollment.pk


def _seed_clinical(graph: RbacGraph, enrollments: tuple[UUID, UUID]) -> None:
    _care_for(graph, list(enrollments))
    request = verified_request(graph.physician)
    appointments = _appointments(graph, enrollments)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        template = publish_template(
            clinic_id=graph.clinic_a,
            key="f6",
            title="F6 Sintético",
            prompts=dict.fromkeys(CONTENT, "Registro sintético"),
        )
        for index, (appointment, enrollment_id) in enumerate(
            zip(appointments, enrollments, strict=True)
        ):
            encounter = open_encounter(
                clinic_id=graph.clinic_a, appointment_id=appointment.pk
            )
            _seed_observation_and_attachment(graph, encounter, enrollment_id, index)
            if index == 0:
                draft = create_draft(
                    clinic_id=graph.clinic_a,
                    encounter_id=encounter.pk,
                    template_id=template.pk,
                )
                saved = record_clinical_note(
                    clinic_id=graph.clinic_a,
                    version_id=draft.pk,
                    expected_revision=draft.revision,
                    content=CONTENT,
                )
                assert (
                    finalize_version(
                        clinic_id=graph.clinic_a,
                        version_id=saved.pk,
                        expected_revision=saved.revision,
                        request=request,
                    ).state
                    == "finalized"
                )


def _appointments(
    graph: RbacGraph, enrollments: tuple[UUID, UUID]
) -> list[Appointment]:
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_availability(
            clinic_id=graph.clinic_a,
            practitioner_id=graph.physician,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
        return [
            arrived_synthetic_appointment(
                AppointmentSetup(
                    graph.organization_a,
                    graph.clinic_a,
                    graph.shared_user,
                    graph.physician,
                    enrollment_id,
                    PatientClinicEnrollment.objects.get(pk=enrollment_id).patient_id,
                ),
                start_local=f"2035-06-02T{hour}:00",
                end_local=f"2035-06-02T{hour + 1}:00",
            )
            for enrollment_id, hour in zip(enrollments, (10, 11), strict=True)
        ]


def _seed_observation_and_attachment(
    graph: RbacGraph, encounter: Encounter, enrollment_id: UUID, index: int
) -> None:
    kind, value, unit = (
        ("temperature", "37.50", "Cel") if index == 0 else ("pain_present", "no", "1")
    )
    record_observation(
        clinic_id=graph.clinic_a,
        enrollment_id=enrollment_id,
        observation=ObservationInput(
            kind=kind,
            value=value,
            unit=unit,
            provenance="observed",
            effective_at=MEASURED,
        ),
    )
    suffix, media, data = (
        ("pdf", "application/pdf", PDF_BYTES)
        if index == 0
        else ("png", "image/png", PNG_BYTES)
    )
    attachment = upload_attachment(
        clinic_id=graph.clinic_a,
        encounter_id=encounter.pk,
        upload=AttachmentInput(
            file_name=f"sintético-{index}.{suffix}", declared_type=media, data=data
        ),
    )
    assert (
        scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk).state
        == "available"
    )


@dataclass(frozen=True)
class Source:
    archive: bytes
    archive_sha256: str
    members: dict[str, bytes]
    manifest: dict[str, object]

    def rows(self, relation: str) -> list[dict[str, object]]:
        return [
            json.loads(raw)
            for raw in self.members[f"data/{relation}.jsonl"].splitlines()
        ]


def _export(graph: RbacGraph, admin: UUID) -> Source:
    pending = _request(graph, admin)
    assert _build(graph, admin, pending) == "ready"
    receipt = _receipt(graph, admin, pending.export_id)
    data = _archive(graph, admin, receipt)
    verification = verify_export_package(data)
    assert verification.ok
    assert verification.manifest_digest == receipt.manifest_sha256
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    assert manifest["v"] == "clinic-organization-export-v5"
    return Source(data, receipt.archive_sha256, members, manifest)


@pytest.fixture
def target(rbac_graph: RbacGraph) -> ImportWorld:
    return import_world(
        replace(
            rbac_graph,
            organization_a=rbac_graph.organization_b,
            clinic_a=rbac_graph.clinic_c,
            physician=rbac_graph.shared_user,
        )
    )


def _upload(
    world: ImportWorld, source: Source, relations: tuple[str, ...]
) -> services.ImportJobView:
    with operating(world):
        return services.upload_v5_import(
            clinic_id=world.graph.clinic_a,
            data=source.archive,
            declared_sha256=source.archive_sha256,
            relations=relations,
            idempotency_key=uuid4(),
        )


def _commit(
    world: ImportWorld, upload: services.ImportJobView, monkeypatch: pytest.MonkeyPatch
) -> services.ReconciliationReport:
    clinic_id, job_id = world.graph.clinic_a, upload.job_id
    with operating(world):
        profile = services.profile_import(
            clinic_id=clinic_id, job_id=job_id, expected_sequence=upload.sequence
        )
        mapping = services.save_mapping(
            clinic_id=clinic_id, code="export-v5", document=services.v5_import_mapping()
        )
        mapped = services.apply_mapping(
            clinic_id=clinic_id,
            job_id=job_id,
            mapping_id=mapping.mapping_id,
            expected_sequence=profile.job.sequence,
        )
        dry_run = services.dry_run_import(
            clinic_id=clinic_id, job_id=job_id, expected_sequence=mapped.sequence
        )
    assert dry_run.job.rejected_count == dry_run.job.duplicate_count == 0
    assert dry_run.job.valid_count == 7
    approved = approve(world, dry_run)
    dispatched: list[dict[str, str]] = []
    monkeypatch.setattr(
        tasks.commit_batch_job,
        "apply_async",
        lambda *, kwargs: dispatched.append(kwargs),
    )
    with operating(world):
        receipt = services.commit_import(
            clinic_id=clinic_id,
            job_id=job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    assert len(receipt.batch_ids) == 3
    assert dispatched == [{"batch_id": str(batch)} for batch in receipt.batch_ids]
    assert run_batches(world, job_id, monkeypatch) == ["committed"] * 3
    with operating(world):
        return services.reconciliation_report(clinic_id=clinic_id, job_id=job_id)


def _digest(values: list[dict[str, object]]) -> str:
    # Sort the canonical row bytes so order cannot disguise value loss,
    # duplicate rows, or a record linked to the other synthetic patient.
    canonical = sorted(rfc8785.dumps(cast("JsonValue", row)) for row in values)
    return sha256(b"\n".join(canonical)).hexdigest()


@dataclass(frozen=True)
class DigestChecks:
    counts: dict[str, int]
    item: pytest.Item

    def record(self, name: str, value: object) -> None:
        self.item.user_properties.append((name, value))

    def equal(
        self,
        relation: str,
        source: list[dict[str, object]],
        target_values: list[dict[str, object]],
    ) -> None:
        count = EXPECTED_COUNTS[relation]
        assert len(source) == self.counts[relation] == len(target_values) == count
        before, after = _digest(source), _digest(target_values)
        assert before == after, relation
        self.record(f"{relation}_source_count", len(source))
        self.record(f"{relation}_target_count", len(target_values))
        self.record(f"{relation}_matched_sha256", before)


def _timestamp(value: object) -> str | None:
    return (
        None
        if value is None
        else datetime.fromisoformat(str(value)).astimezone(UTC).isoformat()
    )


def _origin(row: ImportedRecord, patient_source: str) -> dict[str, object]:
    return {
        "id": row.source_record_id,
        "patient_id": patient_source,
        "author": row.original_author,
        "time": _timestamp(row.original_recorded_at),
    }


def _check_patients(
    source: Source, patients: list[ImportedRecord], checks: DigestChecks
) -> None:
    patient_ids = {row.patient_id: row.source_record_id for row in patients}
    assert len(patient_ids) == len(patients)
    assert not set(map(str, patient_ids)) & set(patient_ids.values())
    assert all(row.enrollment.patient_id == row.patient_id for row in patients)
    corrections = {
        row["patient_id"]: row for row in source.rows("intake_demographicscorrection")
    }
    checks.equal(
        "intake_patient",
        [
            {
                "id": row["id"],
                "full_name": row["full_name"],
                "birth_date": row["birth_date"],
                "author": corrections[row["id"]]["actor_label"],
                "time": _timestamp(row["created_at"]),
            }
            for row in source.rows("intake_patient")
        ],
        [
            {
                "id": row.source_record_id,
                "full_name": row.patient.full_name,
                "birth_date": row.patient.birth_date.isoformat()
                if row.patient.birth_date
                else None,
                "author": row.original_author,
                "time": _timestamp(row.original_recorded_at),
            }
            for row in patients
        ],
    )
    demographics = PatientDemographics.objects.filter(patient_id__in=patient_ids)
    assert all(row.version == 1 for row in demographics)
    fields = ("patient_id", "legal_name", "social_name", "sex_at_birth", "birth_date")
    checks.equal(
        "intake_patientdemographics",
        [
            {name: row[name] for name in fields}
            for row in source.rows("intake_patientdemographics")
        ],
        [
            {
                "patient_id": patient_ids[row.patient_id],
                # The encrypted text codec reads SQL NULL as "" in the ORM;
                # export JSONL reads the same database value as null.
                "legal_name": row.legal_name or None,
                "social_name": row.social_name or None,
                "sex_at_birth": row.sex_at_birth or None,
                "birth_date": row.birth_date.isoformat() if row.birth_date else None,
            }
            for row in demographics
        ],
    )
    identifiers = PatientIdentifier.objects.filter(patient_id__in=patient_ids)
    identifier_fields = ("patient_id", "kind", "value")
    checks.equal(
        "intake_patientidentifier",
        [
            {name: row[name] for name in identifier_fields}
            for row in source.rows("intake_patientidentifier")
        ],
        [
            {
                "patient_id": patient_ids[row.patient_id],
                "kind": row.kind,
                "value": row.value,
            }
            for row in identifiers
        ],
    )


def _source_notes(source: Source) -> list[dict[str, object]]:
    documents = {
        row["id"]: row["encounter_id"] for row in source.rows("ehr_clinicaldocument")
    }
    encounters = {row["id"]: row["patient_id"] for row in source.rows("ehr_encounter")}
    return [
        {
            "id": row["id"],
            "patient_id": encounters[documents[row["document_id"]]],
            "author": row["author_id"],
            "time": _timestamp(row["finalized_at"]),
            "content": {
                "text": rfc8785.dumps(cast("JsonValue", row["content"])).decode()
            },
        }
        for row in source.rows("ehr_clinicaldocumentversion")
    ]


def _source_observations(source: Source) -> list[dict[str, object]]:
    return [
        {
            "id": row["id"],
            "patient_id": row["patient_id"],
            "author": row["performer_id"],
            "time": _timestamp(row["effective_at"]),
            "content": {
                "code": row["kind"],
                "value": part(row, "value", "value"),
                "unit": row["unit"],
                "code_set_version": row["code_set_version"],
            },
        }
        for row in source.rows("ehr_observation")
    ]


def _source_attachments(source: Source) -> list[dict[str, object]]:
    return [
        {
            "id": row["id"],
            "patient_id": row["patient_id"],
            "author": row["uploader_id"],
            "time": _timestamp(row["created_at"]),
            "content": {
                "title": row["file_name"],
                "media_type": row["detected_type"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
            },
        }
        for row in source.rows("ehr_clinicalattachment")
    ]


def _check_history(
    source: Source, world: ImportWorld, job_id: UUID, checks: DigestChecks
) -> None:
    graph = world.graph
    with owner_context(graph.organization_a):
        assert (
            Patient.objects.filter(organization_id=graph.organization_a).count()
            == EXPECTED_COUNTS["intake_patient"]
        )
        assert (
            PatientClinicEnrollment.objects.filter(
                organization_id=graph.organization_a
            ).count()
            == EXPECTED_COUNTS["intake_patient"]
        )
        patients = list(
            ImportedRecord.objects.filter(job_id=job_id, kind="patient").select_related(
                "patient", "enrollment"
            )
        )
        history = list(ImportedRecord.objects.filter(job_id=job_id, dataset="history"))
        _check_patients(source, patients, checks)
        for model in (
            Encounter,
            ClinicalDocumentVersion,
            Observation,
            ClinicalAttachment,
        ):
            assert not model.objects.filter(
                organization_id=graph.organization_a
            ).exists()
    patient_source = {row.enrollment_id: row.source_record_id for row in patients}
    assert len(patient_source) == len(patients)
    _care_for(graph, list(patient_source))
    with operating(world, graph.physician):
        readback = {
            item.record_id: item
            for enrollment_id in patient_source
            for item in services.imported_history(
                clinic_id=graph.clinic_a, enrollment_id=enrollment_id
            )
        }
        assert readback.keys() == {row.pk for row in history}
        target_values: dict[str, list[dict[str, object]]] = {
            "note": [],
            "observation": [],
            "attachment": [],
        }
        for row in history:
            item = readback[row.pk]
            assert item.kind == row.kind
            assert item.provenance == "imported"
            assert item.original_author == row.original_author
            assert item.original_recorded_at == row.original_recorded_at
            assert item.original_author not in {
                str(world.operator),
                str(graph.physician),
            }
            target_values[item.kind].append(
                {
                    **_origin(row, patient_source[row.enrollment_id]),
                    "content": item.content,
                }
            )
            if item.kind == "attachment":
                _check_attachment_bytes(source, graph.clinic_a, row, checks)
    checks.equal(
        "ehr_clinicaldocumentversion", _source_notes(source), target_values["note"]
    )
    checks.equal(
        "ehr_observation", _source_observations(source), target_values["observation"]
    )
    checks.equal(
        "ehr_clinicalattachment",
        _source_attachments(source),
        target_values["attachment"],
    )


def _check_attachment_bytes(
    source: Source,
    clinic_id: UUID,
    row: ImportedRecord,
    checks: DigestChecks,
) -> None:
    downloaded = services.imported_attachment(clinic_id=clinic_id, record_id=row.pk)
    suffix = {"application/pdf": "pdf", "image/png": "png"}[downloaded.media_type]
    path = f"attachments/{row.source_record_id}.{suffix}"
    data = source.members[path]
    (entry,) = [file for file in part(source.manifest, "files") if file["path"] == path]
    assert data == downloaded.data
    assert data == {"pdf": PDF_BYTES, "png": PNG_BYTES}[suffix]
    assert len(data) == entry["bytes"]
    assert (
        sha256(data).hexdigest()
        == sha256(downloaded.data).hexdigest()
        == entry["sha256"]
        == downloaded.sha256
    )
    checks.record(f"attachment_{suffix}_matched_sha256", downloaded.sha256)
    checks.record(f"attachment_{suffix}_bytes", len(data))


@pytest.mark.usefixtures("attachment_root")
def test_v5_roundtrip_checks_every_importable_dataset(
    rbac_graph: RbacGraph,
    target: ImportWorld,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    assert EXPECTED_COUNTS.keys() == IMPORTABLE_DATASETS
    admin = org_admin(rbac_graph)
    enrollments = _seed_identities(rbac_graph)
    _seed_clinical(rbac_graph, enrollments)
    source = _export(rbac_graph, admin)
    job = _upload(target, source, tuple(EXPECTED_COUNTS))
    report = _commit(target, job, monkeypatch)
    assert report.reconciled
    assert report.job.state == "committed"
    assert part(report.report, "datasets") == {
        "patients": {"rows": 2, "accepted": 2, "rejected": 0, "committed": 2},
        "history": {"rows": 5, "accepted": 5, "rejected": 0, "committed": 5},
    }
    assert part(report.report, "checksums") == {
        "source_sha256": job.source_sha256,
        "source_verified": True,
        "attachments_verified": 2,
        "attachment_mismatches": 0,
        "batches_verified": True,
    }
    assert part(report.report, "bytes", "attachments") == len(PDF_BYTES) + len(
        PNG_BYTES
    )
    assert part(report.report, "bytes", "source") == job.size_bytes
    assert isinstance(request.node, pytest.Item)
    checks = DigestChecks(
        {row["relation"]: row["records"] for row in part(source.manifest, "datasets")},
        request.node,
    )
    _check_history(source, target, job.job_id, checks)
    for organization in (rbac_graph.organization_a, target.graph.organization_a):
        with owner_context(organization):
            assert verify_chain(organization).valid


def _refusal_input(source: Source, case: str) -> tuple[bytes, tuple[str, ...]]:
    relations = ("intake_patient",)
    if case == "export_only":
        assert source.rows("identity_userclinicrole")
        return source.archive, (*relations, "identity_userclinicrole")
    manifest = json.loads(rfc8785.dumps(cast("JsonValue", source.manifest)))
    members = source.members.copy()
    patient_file = "data/intake_patient.jsonl"
    if case == "version":
        manifest["v"] = "clinic-organization-export-v4"
    elif case == "member_checksum":
        [entry] = [
            entry
            for entry in manifest["files"]
            if entry["path"] == "data/identity_userclinicrole.jsonl"
        ]
        entry["sha256"] = "0" * 64
    elif case == "truncated_member":
        assert len(members[patient_file]) > 1
        members[patient_file] = members[patient_file][:-1]
    del manifest["manifest_sha256"]
    manifest["manifest_sha256"] = (
        "0" * 64
        if case == "manifest_checksum"
        else sha256(rfc8785.dumps(manifest)).hexdigest()
    )
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("manifest.json", rfc8785.dumps(manifest))
        for name, raw in members.items():
            archive.writestr(name, raw)
    return data.getvalue(), relations


def _target_counts(organization: UUID) -> tuple[int, ...]:
    with owner_context(organization):
        return tuple(
            model.objects.filter(organization_id=organization).count()
            for model in (
                ImportJob,
                ImportMapping,
                ImportTransition,
                ImportBatch,
                ImportFile,
                ImportedRecord,
                Patient,
                PatientClinicEnrollment,
                PatientDemographics,
                DemographicsCorrection,
                PatientIdentifier,
                Encounter,
                ClinicalDocumentVersion,
                ClinicalAttachment,
                Observation,
                UserClinicRole,
                AuditEvent,
            )
        )


@pytest.mark.parametrize(
    ("case", "error", "code"),
    [
        ("version", ImportInputError, Code.MANIFEST_MISMATCH),
        ("manifest_checksum", ImportConflictError, Code.CHECKSUM_MISMATCH),
        ("member_checksum", ImportConflictError, Code.CHECKSUM_MISMATCH),
        ("truncated_member", ImportConflictError, Code.CHECKSUM_MISMATCH),
        ("export_only", ImportInputError, Code.UNSUPPORTED_DATASET),
    ],
)
def test_v5_refuses_without_partial_writes(
    rbac_graph: RbacGraph,
    target: ImportWorld,
    case: str,
    error: type[ImportInputError | ImportConflictError],
    code: Code,
) -> None:
    _seed_identities(rbac_graph)
    source = _export(rbac_graph, org_admin(rbac_graph))
    data, relations = _refusal_input(source, case)
    before = _target_counts(target.graph.organization_a)
    # Catch *inside* the tenant transaction, so the outer transaction commits.
    # A premature successful write is not hidden by exception-triggered rollback.
    with operating(target), pytest.raises(error) as refused:
        services.upload_v5_import(
            clinic_id=target.graph.clinic_a,
            data=data,
            declared_sha256=sha256(data).hexdigest(),
            relations=relations,
            idempotency_key=uuid4(),
        )
    assert str(refused.value) == code
    assert _target_counts(target.graph.organization_a) == before
