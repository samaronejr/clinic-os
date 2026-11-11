"""Todo 66 acceptance: dry run, checksums, provenance, reconciliation, rollback."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import CareTeamMembership, ProfessionalRegistration
from apps.intake.models import (
    DuplicateCandidate,
    Patient,
    PatientClinicEnrollment,
    PatientDemographics,
)
from apps.intake.patient_search import search_patients_exact
from apps.intake.services import search_patient_identifiers, update_demographics
from apps.interop import services
from apps.interop.models import HiddenEnrollment, ImportedRecord, ImportFile
from django.db import connection
from django.utils import timezone

from identity.permission_support import owner_context
from interop.support import (
    PATIENT_HEADER,
    SYNTHETIC_PDF,
    archive,
    committed,
    cpf,
    csv_bytes,
    history_csv,
    history_row,
    import_world,
    operating,
    part,
    patient_row,
    patients_csv,
    run_batches,
    to_dry_run,
    upload,
)

if TYPE_CHECKING:
    from interop.support import ImportWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _superuser_count(database_url: str, sql: str, params: tuple[object, ...]) -> int:
    with psycopg.connect(database_url) as raw:
        row = raw.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def test_ten_thousand_patient_csv_dry_run_counts_reconcile(
    rbac_graph: RbacGraph,
) -> None:
    world = import_world(rbac_graph)
    malformed = [
        patient_row(10_001, CPF="111.111.111-11"),
        patient_row(10_002, CPF="123.456.789-00"),
        patient_row(10_003, Nascimento="31/02/1990"),
        patient_row(10_004, Nascimento="01/01/2999"),
        patient_row(10_005, Nome="", **{"Nome social": ""}),
        patient_row(10_006, Sexo="X"),
        patient_row(1, Codigo="SIN-010007"),  # same CPF as row 1
        patient_row(10_008, Codigo="SIN-000002"),  # repeated legacy code
    ]
    source = patients_csv(10_000, extra=malformed)

    report = to_dry_run(world, source)

    datasets = part(report.report, "datasets")
    assert datasets == {"patients": {"rows": 10_008, "accepted": 10_000, "rejected": 8}}
    assert report.job.row_count == 10_008
    assert report.job.valid_count + report.job.rejected_count == report.job.row_count
    reasons = {(item[2], item[3]) for item in part(report.report, "rejections")}
    assert reasons == {
        ("cpf", "invalid_cpf"),
        ("birth_date", "invalid_date"),
        ("birth_date", "future_date"),
        ("legal_name", "required"),
        ("sex_at_birth", "invalid_choice"),
        ("cpf", "duplicate_identifier"),
        ("source_id", "duplicate_source_id"),
    }
    assert report.job.state == "dry_run"
    assert report.job.duplicate_count == 0
    # The digest binds the exact accepted rows: a second dry run reproduces it.
    with operating(world):
        remapped = services.apply_mapping(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            mapping_id=cast("UUID", report.job.mapping_id),
            expected_sequence=report.job.sequence,
        )
        again = services.dry_run_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=remapped.sequence,
        )
    assert again.digest == report.digest
    assert again.report == report.report


def test_checksum_mismatch_fails_before_and_after_upload(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = import_world(rbac_graph)
    source = patients_csv(3)
    with operating(world), pytest.raises(services.ImportConflictError) as declared:
        services.upload_import(
            clinic_id=rbac_graph.clinic_a,
            source_kind="csv",
            data=source,
            declared_sha256=hashlib.sha256(source + b"x").hexdigest(),
            idempotency_key=uuid4(),
        )
    assert declared.value.code == "checksum_mismatch"
    members = {"patients.csv": patients_csv(2)}
    tampered = archive(
        members,
        manifest_override={
            "v": "clinic-import-archive-v1",
            "files": [
                {"path": "patients.csv", "sha256": "0" * 64, "bytes": 1},
            ],
        },
    )
    with operating(world), pytest.raises(services.ImportConflictError) as member:
        services.upload_import(
            clinic_id=rbac_graph.clinic_a,
            source_kind="zip",
            data=tampered,
            declared_sha256=hashlib.sha256(tampered).hexdigest(),
            idempotency_key=uuid4(),
        )
    assert member.value.code == "checksum_mismatch"
    with owner_context(rbac_graph.organization_a):
        assert not ImportFile.objects.exists()

    # A stored source that no longer matches its upload digest never commits.
    report = to_dry_run(world, source)
    other = upload(world, patients_csv(4))
    with psycopg.connect(superuser_database_url) as raw:
        raw.execute("ALTER TABLE clinic_app.interop_importfile DISABLE TRIGGER USER")
        raw.execute(
            "UPDATE clinic_app.interop_importfile SET data=("
            "SELECT data FROM clinic_app.interop_importfile WHERE job_id=%s) "
            "WHERE job_id=%s AND role='source'",
            (other.job_id, report.job.job_id),
        )
        raw.execute("ALTER TABLE clinic_app.interop_importfile ENABLE TRIGGER USER")
    approved = _approve(world, report)
    with operating(world), pytest.raises(services.ImportConflictError) as stored:
        services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    assert stored.value.code == "checksum_mismatch"
    assert run_batches(world, report.job.job_id, monkeypatch) == []


def _approve(
    world: ImportWorld, report: services.DryRunReport
) -> services.ImportJobView:
    with operating(world):
        return services.approve_import(
            clinic_id=world.graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=report.job.sequence,
            plan=services.CutoverPlan(
                rollback_window_hours=24,
                source_frozen=True,
                backup_verified=True,
                rejects_acknowledged=True,
                duplicates_acknowledged=True,
            ),
        )


def _visible_enrollments(world: ImportWorld, job_id: UUID) -> int:
    with operating(world):
        enrollments = ImportedRecord.objects.filter(
            job_id=job_id, kind="patient"
        ).values_list("enrollment_id", flat=True)
        return PatientClinicEnrollment.objects.filter(pk__in=list(enrollments)).count()


def test_rollback_hides_batch_and_retains_used_patients(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = import_world(rbac_graph)
    report, receipt = committed(world, patients_csv(6), monkeypatch)
    assert len(receipt.batch_ids) == 1
    assert _visible_enrollments(world, report.job.job_id) == 6
    with operating(world):
        reconciliation = services.reconciliation_report(
            clinic_id=rbac_graph.clinic_a, job_id=report.job.job_id
        )
    assert reconciliation.reconciled is True
    assert reconciliation.job.state == "committed"
    assert part(reconciliation.report, "datasets") == {
        "patients": {"rows": 6, "accepted": 6, "rejected": 0, "committed": 6}
    }
    assert part(reconciliation.report, "checksums", "source_verified") is True
    assert part(reconciliation.report, "bytes", "source") == report.job.size_bytes

    # A receptionist corrects one imported patient: it is now in use.
    reception = import_world(rbac_graph, "receptionist")
    with owner_context(rbac_graph.organization_a):
        used = PatientClinicEnrollment.objects.get(
            pk=ImportedRecord.objects.get(
                job_id=report.job.job_id, row_number=1
            ).enrollment_id
        )
    with operating(reception):
        visible = search_patient_identifiers(
            clinic_id=rbac_graph.clinic_a, kind="cpf", value=cpf(2)
        )
        assert visible.total == 1
    with operating(world, reception.operator):
        update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=used.pk,
            expected_version=1,
            changes={"social_name": "Nome Social Sintetico"},
            reason="Correcao sintetica",
        )

    with operating(world):
        rolled = services.rollback_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=reconciliation.job.sequence,
            reason="data_quality",
            session_key=world.session,
        )
    assert rolled.job.state == "rolled_back"
    assert rolled.hidden == 5
    assert rolled.retained_enrollment_ids == (used.pk,)
    assert _visible_enrollments(world, report.job.job_id) == 1
    with operating(reception):
        hidden = search_patient_identifiers(
            clinic_id=rbac_graph.clinic_a, kind="cpf", value=cpf(2)
        )
        assert hidden.total == 0
        assert (
            search_patients_exact(
                clinic_id=rbac_graph.clinic_a,
                query="Paciente Sintetico 000002",
                limit=5,
            ).total
            == 0
        )
        assert (
            search_patients_exact(
                clinic_id=rbac_graph.clinic_a,
                query="Paciente Sintetico 000001",
                limit=5,
            ).total
            == 1
        )
    # Nothing was deleted: every row is physically retained.
    assert (
        _superuser_count(
            superuser_database_url,
            "SELECT count(*) FROM clinic_app.intake_patientclinicenrollment e "
            "JOIN clinic_app.interop_importedrecord r ON r.enrollment_id=e.id "
            "WHERE r.job_id=%s",
            (report.job.job_id,),
        )
        == 6
    )
    with owner_context(rbac_graph.organization_a):
        assert HiddenEnrollment.objects.filter(job_id=report.job.job_id).count() == 5
        assert (
            Patient.objects.filter(
                pk__in=ImportedRecord.objects.filter(job_id=report.job.job_id).values(
                    "patient_id"
                )
            ).count()
            == 6
        )
    with operating(world), pytest.raises(services.ImportConflictError) as again:
        services.rollback_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=rolled.job.sequence,
            reason="data_quality",
            session_key=world.session,
        )
    assert again.value.code == "interop_transition"


def test_provenance_keeps_original_author_and_time_never_the_importer(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    members = {
        "patients.csv": patients_csv(2),
        "history.csv": history_csv(
            [
                history_row(
                    "EV-1",
                    "SIN-000001",
                    "Evolucao",
                    text='=HYPERLINK("https://exemplo.invalid","x") nota antiga',
                ),
                history_row(
                    "SV-1",
                    "SIN-000001",
                    "Sinal vital",
                    measure=("temperature", "37,5", "Cel"),
                    author="Enf. Sintetica Origem",
                    when="13/02/2020 08:00",
                ),
                history_row("AX-1", "SIN-000002", "Anexo", file="laudo.pdf"),
            ]
        ),
        "files/laudo.pdf": SYNTHETIC_PDF,
    }
    report, _receipt = committed(world, archive(members), monkeypatch, kind="zip")
    assert part(report.report, "datasets", "history") == {
        "rows": 3,
        "accepted": 3,
        "rejected": 0,
    }
    with owner_context(rbac_graph.organization_a):
        note = ImportedRecord.objects.get(job_id=report.job.job_id, kind="note")
        first = ImportedRecord.objects.get(
            job_id=report.job.job_id, dataset="patients", row_number=1
        )
        receipt = PatientDemographics.objects.get(patient_id=first.patient_id)
    assert note.original_author == "Dra. Sintetica Origem"
    assert note.original_recorded_at is not None
    assert note.original_recorded_at.year == 2020
    assert first.original_author == "Recepcao Sintetica Legada"
    assert first.source_record_id == "SIN-000001"
    assert receipt.version == 1

    physician = import_world(rbac_graph, "physician").operator
    with owner_context(rbac_graph.organization_a):
        ProfessionalRegistration.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=physician,
            role="physician",
            council="CRM",
            number="SINTETICO-066",
            jurisdiction="SP",
            specialty="Sintetico",
            status="regular",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
        CareTeamMembership.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_enrollment_id=note.enrollment_id,
            user_id=physician,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
    with operating(world, physician):
        history = services.imported_history(
            clinic_id=rbac_graph.clinic_a, enrollment_id=note.enrollment_id
        )
    assert [item.kind for item in history] == ["note", "observation"]
    assert {item.provenance for item in history} == {"imported"}
    assert history[0].original_author == "Dra. Sintetica Origem"
    assert history[1].content == {
        "code": "temperature",
        "value": "37.5",
        "unit": "Cel",
        "code_set_version": 1,
    }
    assert all(physician != item.original_author for item in history)
    # The importer may run the migration but holds no clinical read authority.
    with operating(world), pytest.raises(services.ImportAccessDeniedError):
        services.imported_history(
            clinic_id=rbac_graph.clinic_a, enrollment_id=note.enrollment_id
        )


def test_malformed_rows_are_rejected_with_row_level_reasons(
    rbac_graph: RbacGraph,
) -> None:
    world = import_world(rbac_graph)
    members = {
        "patients.csv": csv_bytes(
            PATIENT_HEADER,
            [
                patient_row(1),
                patient_row(2, Nascimento="1990-01-01"),
                ["SIN-short", "So tres", "colunas"],
            ],
        ),
        "history.csv": history_csv(
            [
                history_row(
                    "H-1", "SIN-000001", "Sinal vital", measure=("spo2", "97", "kg")
                ),
                history_row(
                    "H-2", "SIN-000001", "Sinal vital", measure=("glucose", "9", "%")
                ),
                history_row(
                    "H-3", "SIN-000001", "Sinal vital", measure=("spo2", "noventa", "%")
                ),
                history_row(
                    "H-4", "SIN-999999", "Evolucao", text="Paciente inexistente"
                ),
                history_row(
                    "H-5", "SIN-000001", "Evolucao", when="31/12/2999 10:00", text="x"
                ),
                history_row("H-6", "SIN-000001", "Anexo", file="ausente.pdf"),
                history_row("H-7", "SIN-000001", "Desconhecido", text="x"),
            ]
        ),
    }
    report = to_dry_run(world, archive(members), kind="zip")
    rejections = {
        (item[0], item[1], item[2], item[3])
        for item in part(report.report, "rejections")
    }
    assert rejections == {
        ("patients", 2, "birth_date", "invalid_date"),
        ("patients", 3, "row", "column_count"),
        ("history", 1, "unit", "invalid_unit"),
        ("history", 2, "code", "invalid_code"),
        ("history", 3, "value", "invalid_value"),
        ("history", 4, "patient_source_id", "unknown_patient"),
        ("history", 5, "recorded_at", "future_date"),
        ("history", 6, "file", "missing_file"),
        ("history", 7, "kind", "invalid_kind"),
    }
    assert part(report.report, "datasets") == {
        "patients": {"rows": 3, "accepted": 1, "rejected": 2},
        "history": {"rows": 7, "accepted": 0, "rejected": 7},
    }
    with operating(world):
        exported = services.export_import_report(
            clinic_id=rbac_graph.clinic_a, job_id=report.job.job_id
        )
    lines = list(csv.reader(io.StringIO(exported.decode("utf-8"))))
    assert lines[0] == ["dataset", "row", "source_id", "field", "code"]
    assert ["history", "1", "H-1", "unit", "invalid_unit"] in lines


def test_formula_injection_is_neutralized_in_exports(rbac_graph: RbacGraph) -> None:
    world = import_world(rbac_graph)
    hostile = '=HYPERLINK("https://exemplo.invalid/x","clique")'
    source = csv_bytes(
        PATIENT_HEADER,
        [
            patient_row(1),
            patient_row(2, Codigo=hostile, CPF="000"),
            patient_row(3, Codigo="+SUM(1,1)", Nome="@Paciente"),
            patient_row(4, Codigo="-2+3", Nome=hostile),
        ],
    )
    report = to_dry_run(world, source)
    codes = {(item[1], item[2], item[3]) for item in part(report.report, "rejections")}
    assert codes == {
        (2, "cpf", "invalid_cpf"),
        (3, "legal_name", "formula_like"),
        (4, "legal_name", "formula_like"),
    }
    with operating(world):
        exported = services.export_import_report(
            clinic_id=rbac_graph.clinic_a, job_id=report.job.job_id
        ).decode("utf-8")
    cells = [cell for line in csv.reader(io.StringIO(exported)) for cell in line]
    assert "'" + hostile in cells
    assert "'+SUM(1,1)" in cells
    assert "'-2+3" in cells
    assert not any(cell[:1] in "=+-@" for cell in cells)


def test_duplicates_route_to_human_review_and_never_merge(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    committed(world, patients_csv(2), monkeypatch)
    second = csv_bytes(
        PATIENT_HEADER, [patient_row(1, Codigo="NOVO-1"), patient_row(77)]
    )
    report, _receipt = committed(world, second, monkeypatch)
    assert report.job.duplicate_count == 1
    assert part(report.report, "duplicates") == [
        [1, ["dob+name_prefix", "exact_identifier"]]
    ]
    with owner_context(rbac_graph.organization_a):
        candidates = DuplicateCandidate.objects.filter(clinic_id=rbac_graph.clinic_a)
        assert candidates.count() == 1
        assert set(candidates.get().signals) == {"exact_identifier", "dob+name_prefix"}
        assert candidates.get().state == "open"
        assert Patient.objects.count() == 4
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
