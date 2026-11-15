"""Independent source-byte and provenance oracle for the synthetic S16 CSV."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apps.identity.models import Clinic
from apps.intake.models import PatientDemographics, PatientIdentifier
from apps.interop import services
from apps.interop.models import ImportedRecord, ImportFile, ImportJob
from apps.interop.validation import canonical_bytes

from identity.permission_support import owner_context
from interop.support import operating

if TYPE_CHECKING:
    from uuid import UUID

    from interop.support import ImportWorld


def reconcile_source(
    world: ImportWorld, job_id: UUID, source: bytes, accepted: list[list[str]]
) -> dict[str, object]:
    """Compare persisted evidence with independent expectations from input rows."""
    source_hash = sha256(source).hexdigest()
    row_count = (
        len(list(csv.reader(io.StringIO(source.decode("utf-8-sig")), delimiter=";")))
        - 1
    )
    with operating(world):
        report = services.reconciliation_report(
            clinic_id=world.graph.clinic_a, job_id=job_id
        )
        job = ImportJob.objects.get(pk=job_id)
        assert job.source_sha256 == source_hash
        assert job.size_bytes == len(source)
    assert report.reconciled is True
    assert report.job.state == "committed"
    assert report.digest == sha256(canonical_bytes(report.report)).hexdigest()
    assert report.report["datasets"] == {
        "patients": {
            "rows": row_count,
            "accepted": len(accepted),
            "rejected": row_count - len(accepted),
            "committed": len(accepted),
        }
    }
    assert report.report["bytes"] == {"source": len(source), "attachments": 0}
    assert report.report["checksums"] == {
        "source_sha256": source_hash,
        "source_verified": True,
        "attachments_verified": 0,
        "attachment_mismatches": 0,
        "batches_verified": True,
    }
    # Owner access is an independent read-only oracle for encrypted provenance;
    # upload, approval, commit and the report above use the real operator role.
    with owner_context(world.graph.organization_a):
        # The pipeline source resolver intentionally closes after commit.
        # Inspect retained bytes as the owner without widening runtime access.
        stored = ImportFile.objects.get(job_id=job_id, role="source")
        assert stored.data == source
        assert stored.sha256 == source_hash
        assert stored.size_bytes == len(source)
        timezone = Clinic.objects.get(pk=world.graph.clinic_a).timezone
        assert isinstance(timezone, str)
        zone = ZoneInfo(timezone)
        records = list(
            ImportedRecord.objects.filter(job_id=job_id)
            .select_related("patient", "enrollment", "batch")
            .order_by("row_number")
        )
        assert len(records) == len(accepted)
        expected = [
            _reconcile_record(record, row, number, zone)
            for number, (record, row) in enumerate(
                zip(records, accepted, strict=True), 1
            )
        ]
    assert report.report["batches"] == [
        {
            "sequence": 1,
            "dataset": "patients",
            "rows": len(accepted),
            "committed": len(accepted),
            "rows_sha256": sha256(canonical_bytes(expected)).hexdigest(),
            "verified": True,
        }
    ]
    return {
        "source_sha256": source_hash,
        "source_bytes": len(source),
        "retained_source_matches": True,
        "report_digest_verified": True,
        "provenance_records_matched": len(records),
        "demographics_records_matched": len(records),
        "batch_hashes_match_source": True,
    }


def _reconcile_record(
    record: ImportedRecord, row: list[str], number: int, zone: ZoneInfo
) -> dict[str, object]:
    code, name, social, born, cpf, sex, registered, author = row
    birth = datetime.strptime(born, "%d/%m/%Y").replace(tzinfo=zone).date()
    recorded = datetime.strptime(registered, "%d/%m/%Y %H:%M").replace(tzinfo=zone)
    gender = {"F": "female", "M": "male"}[sex]
    expected: dict[str, object] = {
        "row": number,
        "source_id": code,
        "legal_name": name,
        "social_name": social or None,
        "birth_date": birth.isoformat(),
        "sex_at_birth": gender,
        "identifiers": [["cpf", cpf]],
        "registered_at": recorded.isoformat(),
        "registered_by": author,
    }
    assert record.row_number == number
    assert record.dataset == "patients"
    assert record.kind == "patient"
    assert record.source_record_id == code
    assert record.original_author == author
    assert record.original_recorded_at == recorded
    assert record.content_sha256 == sha256(canonical_bytes(expected)).hexdigest()
    assert record.content == {
        "fields": sorted(key for key, value in expected.items() if value)
    }
    assert record.identifier_count == 1
    assert record.batch.job_id == record.job_id
    assert record.enrollment.patient_id == record.patient_id
    assert record.enrollment.clinic_id == record.clinic_id
    assert record.patient.full_name == name
    assert record.patient.birth_date == birth
    demographics = PatientDemographics.objects.get(
        patient_id=record.patient_id, version=1
    )
    assert demographics.enrollment_id == record.enrollment_id
    assert demographics.legal_name == name
    assert demographics.social_name in (None, social)
    assert demographics.birth_date == birth
    assert demographics.sex_at_birth == gender
    identifier = PatientIdentifier.objects.get(
        patient_id=record.patient_id, kind="cpf", version=1
    )
    assert identifier.value == cpf
    return expected
