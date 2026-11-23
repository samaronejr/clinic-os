"""Exercise the source row ledger through the governed import services."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.interop import jobs, services, sources, validation
from apps.interop.models import ImportedRecord

from identity.permission_support import owner_context
from interop.support import (
    approve,
    archive,
    history_csv,
    history_row,
    import_world,
    mapping,
    operating,
    part,
    patients_csv,
    run_batches,
    to_dry_run,
    upload,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.identity.models import Clinic

    from interop.support import ImportWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def legacy_zip() -> bytes:
    return archive(
        {
            "patients.csv": patients_csv(1),
            "history.csv": history_csv(
                [history_row("H-1", "SIN-000001", "Evolucao", text="Synthetic note")]
            ),
        }
    )


def _finish(
    world: ImportWorld, signed: services.ImportJobView, patch: pytest.MonkeyPatch
) -> services.ReconciliationReport:
    clinic_id = world.graph.clinic_a
    with operating(world):
        receipt = services.commit_import(
            clinic_id=clinic_id,
            job_id=signed.job_id,
            expected_sequence=signed.sequence,
            session_key=world.session,
        )
    assert run_batches(world, signed.job_id, patch) == ["committed"] * len(
        receipt.batch_ids
    )
    with operating(world):
        return services.reconciliation_report(clinic_id=clinic_id, job_id=signed.job_id)


def test_legacy_zip_requires_acknowledgment_when_history_is_unmapped(
    rbac_graph: RbacGraph, legacy_zip: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    dry = to_dry_run(
        world, legacy_zip, kind="zip", document=mapping("zip", history=False)
    )
    with operating(world), pytest.raises(services.ImportConflictError) as refused:
        services.approve_import(
            clinic_id=world.graph.clinic_a,
            job_id=dry.job.job_id,
            expected_sequence=dry.job.sequence,
            plan=services.CutoverPlan(
                rollback_window_hours=72,
                source_frozen=True,
                backup_verified=True,
                rejects_acknowledged=False,
                duplicates_acknowledged=False,
            ),
        )
    assert refused.value.code == "rejects_unacknowledged"
    assert part(dry.report, "datasets", "history") == {
        "rows": 1,
        "accepted": 0,
        "rejected": 1,
    }
    assert part(dry.report, "rejections") == [
        ["history", 1, "dataset", "unmapped_dataset", ""]
    ]
    report = _finish(world, approve(world, dry), monkeypatch)
    assert report.reconciled is True
    assert part(report.report, "datasets", "history") == {
        "rows": 1,
        "accepted": 0,
        "rejected": 1,
        "committed": 0,
    }


def test_profiling_cannot_drop_a_row_counted_at_upload(
    rbac_graph: RbacGraph, legacy_zip: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    job = upload(world, legacy_zip, kind="zip")
    read_datasets = sources.read_datasets

    def drop_history_row(
        kind: str, data: bytes, options: Mapping[str, sources.CsvOptions] | None = None
    ) -> dict[str, sources.Dataset]:
        parsed = read_datasets(kind, data, options)
        parsed["history"] = replace(parsed["history"], rows=())
        return parsed

    monkeypatch.setattr(sources, "read_datasets", drop_history_row)
    clinic_id = world.graph.clinic_a
    with operating(world):
        with pytest.raises(services.ImportConflictError) as refused:
            services.profile_import(
                clinic_id=clinic_id, job_id=job.job_id, expected_sequence=job.sequence
            )
        assert services.import_job(clinic_id=clinic_id, job_id=job.job_id).state == (
            "uploaded"
        )
    assert refused.value.code == "row_accounting_mismatch"


@pytest.mark.parametrize("same_count", [False, True])
def test_mapping_cannot_regroup_profiled_csv_records(
    rbac_graph: RbacGraph, same_count: bool
) -> None:
    data = b"Codigo;Nome;Tag;Meta,Code,Name\n"
    if same_count:
        # Only semicolon parsing groups this pair into one row.
        data += b'a;"two\nlines";tag;meta,ALT,Alice\n'
    # Only comma parsing groups this pair into one row.
    data += b'b;Alice;tag;meta,"also\ntwo lines",Alice\n'
    comma = sources.CsvOptions(delimiter=",", encoding="utf-8")
    comma_count = len(sources.read_csv("patients", data, comma).rows)
    world = import_world(rbac_graph)
    clinic_id = world.graph.clinic_a
    job = upload(world, data)
    with operating(world):
        profile = services.profile_import(
            clinic_id=clinic_id, job_id=job.job_id, expected_sequence=job.sequence
        )
        profiles_count = part(profile.profile, "datasets", "patients", "row_count")
        assert profiles_count == (3 if same_count else 2)
        assert comma_count == (3 if same_count else 1)
        assert part(profile.profile, "datasets", "patients", "csv", "delimiter") == ";"
        saved = services.save_mapping(
            clinic_id=clinic_id,
            code="alternate-csv",
            document={
                "schema": "import-mapping-v1",
                "source_kind": "csv",
                "csv": {"patients": {"delimiter": ",", "encoding": "utf-8"}},
                "datasets": {
                    "patients": {"columns": {"source_id": "Code", "legal_name": "Name"}}
                },
            },
        )
        with pytest.raises(services.ImportConflictError) as refused:
            services.apply_mapping(
                clinic_id=clinic_id,
                job_id=job.job_id,
                mapping_id=saved.mapping_id,
                expected_sequence=profile.job.sequence,
            )
        assert services.import_job(clinic_id=clinic_id, job_id=job.job_id).state == (
            "profiled"
        )
    assert refused.value.code == "row_accounting_mismatch"


def test_validation_cannot_lose_a_profiled_row_without_an_outcome(
    rbac_graph: RbacGraph, legacy_zip: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drop_normalized_history(
        datasets: Mapping[str, sources.Dataset],
        document: Mapping[str, object],
        *,
        clinic: Clinic,
        attachments: Mapping[str, validation.AttachmentFacts],
        required_fields: tuple[str, ...],
    ) -> validation.Normalized:
        result = validation.normalize(
            datasets,
            document,
            clinic=clinic,
            attachments=attachments,
            required_fields=required_fields,
        )
        assert [row.row for row in result.history] == [1]
        # Keep the correct census: the row simply never receives a disposition.
        return replace(result, history=())

    world = import_world(rbac_graph)
    job = upload(world, legacy_zip, kind="zip")
    clinic_id = world.graph.clinic_a
    with operating(world):
        profile = services.profile_import(
            clinic_id=clinic_id, job_id=job.job_id, expected_sequence=job.sequence
        )
        saved = services.save_mapping(
            clinic_id=clinic_id,
            code="legacy-zip",
            document=mapping("zip", history=True),
        )
        mapped = services.apply_mapping(
            clinic_id=clinic_id,
            job_id=job.job_id,
            mapping_id=saved.mapping_id,
            expected_sequence=profile.job.sequence,
        )
        monkeypatch.setattr(jobs, "normalize", drop_normalized_history)
        with pytest.raises(services.ImportConflictError) as refused:
            services.dry_run_import(
                clinic_id=clinic_id,
                job_id=job.job_id,
                expected_sequence=mapped.sequence,
            )
        assert services.import_job(clinic_id=clinic_id, job_id=job.job_id).state == (
            "mapped"
        )
        with pytest.raises(services.ImportConflictError) as unavailable:
            services.dry_run_report(clinic_id=clinic_id, job_id=job.job_id)
    assert refused.value.code == "row_accounting_mismatch"
    assert unavailable.value.code == "interop_transition"


def test_explicit_history_exclusion_is_acknowledged_and_reconciles(
    rbac_graph: RbacGraph, legacy_zip: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    rule = "operator_excluded_history"
    document = {**mapping("zip", history=False), "history_exclusion": rule}
    dry = to_dry_run(world, legacy_zip, kind="zip", document=document)
    exclusions = {"history": {"rule": rule, "rows": [1]}}
    assert dry.report["rejections"] == []
    assert dry.report["exclusions"] == exclusions
    with operating(world), pytest.raises(services.ImportConflictError) as denied:
        services.approve_import(
            clinic_id=world.graph.clinic_a,
            job_id=dry.job.job_id,
            expected_sequence=dry.job.sequence,
            plan=services.CutoverPlan(
                rollback_window_hours=72,
                source_frozen=True,
                backup_verified=True,
                rejects_acknowledged=False,
                duplicates_acknowledged=False,
            ),
        )
    assert denied.value.code == "rejects_unacknowledged"
    report = _finish(world, approve(world, dry), monkeypatch)

    assert report.reconciled is True
    assert report.report["row_accounting_verified"] is True
    assert report.report["exclusions"] == exclusions
    assert part(report.report, "datasets", "history") == {
        "rows": 1,
        "accepted": 0,
        "rejected": 0,
        "excluded": 1,
        "committed": 0,
    }
    with owner_context(world.graph.organization_a):
        assert list(
            ImportedRecord.objects.filter(job_id=dry.job.job_id).values_list(
                "dataset", "row_number"
            )
        ) == [("patients", 1)]


def test_blank_csv_record_is_counted_and_rejected(rbac_graph: RbacGraph) -> None:
    world = import_world(rbac_graph)
    dry = to_dry_run(world, patients_csv(1) + b"\r\n")

    assert part(dry.report, "datasets", "patients") == {
        "rows": 2,
        "accepted": 1,
        "rejected": 1,
    }
    assert dry.report["rejections"] == [["patients", 2, "row", "blank_row", ""]]
