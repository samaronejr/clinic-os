"""Reconciliation must account for source rows, not just match totals."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.interop import commit, jobs, services
from apps.interop.models import ImportedRecord
from apps.interop.validation import normalize

from identity.permission_support import owner_context
from interop.support import (
    approve,
    archive,
    committed,
    history_csv,
    history_row,
    import_world,
    operating,
    part,
    patients_csv,
    run_batches,
    to_dry_run,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from apps.identity.models import Clinic
    from apps.interop.models import ImportBatch
    from apps.interop.sources import Dataset
    from apps.interop.validation import AttachmentFacts, HistoryRow, Normalized

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def history_with_reject() -> bytes:
    return archive(
        {
            "patients.csv": patients_csv(1),
            "history.csv": history_csv(
                [
                    history_row("H-1", "SIN-000001", "Evolucao", text="Nota sintética"),
                    history_row(
                        "H-2",
                        "SIN-000001",
                        "Evolucao",
                        text="Nota sintética",
                        when="31/02/1990 14:05",
                    ),
                    history_row("H-3", "SIN-000001", "Evolucao", text="Outra nota"),
                ]
            ),
        }
    )


def test_reconciliation_rejects_an_accepted_row_not_written_by_the_batch(
    rbac_graph: RbacGraph, history_with_reject: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    write_history = commit._commit_history

    def write_only_first(
        *, clinic: Clinic, batch: ImportBatch, rows: Sequence[HistoryRow]
    ) -> int:
        return write_history(clinic=clinic, batch=batch, rows=rows[:1])

    monkeypatch.setattr(commit, "_commit_history", write_only_first)
    dry_run, _receipt = committed(world, history_with_reject, monkeypatch, kind="zip")
    with operating(world):
        report = services.reconciliation_report(
            clinic_id=world.graph.clinic_a, job_id=dry_run.job.job_id
        )
    with owner_context(world.graph.organization_a):
        saved = list(
            ImportedRecord.objects.filter(job_id=dry_run.job.job_id, dataset="history")
            .order_by("row_number")
            .values_list("row_number", flat=True)
        )

    assert saved == [1]
    assert part(report.report, "datasets", "history") == {
        "rows": 3,
        "accepted": 2,
        "rejected": 1,
        "committed": 1,
    }
    assert part(report.report, "canonical_values", "committed_values_verified") is False
    assert report.report["row_accounting_verified"] is False
    assert report.reconciled is False


def test_reconciliation_rejects_a_row_counted_as_both_committed_and_rejected(
    rbac_graph: RbacGraph, history_with_reject: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    dry_run = to_dry_run(world, history_with_reject, kind="zip")
    signed = approve(world, dry_run)
    clinic_id = world.graph.clinic_a
    with operating(world):
        receipt = services.commit_import(
            clinic_id=clinic_id,
            job_id=signed.job_id,
            expected_sequence=signed.sequence,
            session_key=world.session,
        )

    def overlap(
        datasets: Mapping[str, Dataset],
        mapping: Mapping[str, object],
        *,
        clinic: Clinic,
        attachments: Mapping[str, AttachmentFacts],
        required_fields: tuple[str, ...],
    ) -> Normalized:
        parsed = normalize(
            datasets,
            mapping,
            clinic=clinic,
            attachments=attachments,
            required_fields=required_fields,
        )
        (rejected,) = parsed.rejections
        # Corrupt the replay after planning. Row 1 is counted twice, replacing
        # the rejection for row 2 without changing totals or the planned writes.
        return replace(parsed, rejections=(replace(rejected, row=1),))

    monkeypatch.setattr(jobs, "normalize", overlap)
    assert run_batches(world, signed.job_id, monkeypatch) == ["committed"] * len(
        receipt.batch_ids
    )
    with operating(world):
        report = services.reconciliation_report(
            clinic_id=world.graph.clinic_a, job_id=dry_run.job.job_id
        )
    with owner_context(world.graph.organization_a):
        saved = list(
            ImportedRecord.objects.filter(job_id=dry_run.job.job_id, dataset="history")
            .order_by("row_number")
            .values_list("row_number", flat=True)
        )

    assert saved == [1, 3]
    assert part(dry_run.report, "rejections")[0][:2] == ["history", 2]
    assert part(report.report, "datasets", "history") == {
        "rows": 3,
        "accepted": 2,
        "rejected": 1,
        "committed": 2,
    }
    assert part(report.report, "canonical_values", "committed_values_verified") is True
    assert part(report.report, "checksums", "batches_verified") is True
    assert report.report["row_accounting_verified"] is False
    assert report.reconciled is False
