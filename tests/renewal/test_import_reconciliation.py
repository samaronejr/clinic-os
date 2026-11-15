"""S16 compares real retained imports against independent source expectations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from interop.support import committed, import_world, patient_row, patients_csv
from renewal.import_reconciliation_fixture import reconcile_source

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "mismatch", ["none", "bytes", "source_id", "author", "time", "name"]
)
def test_reconciliation_rejects_wrong_source_or_provenance(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    world = import_world(rbac_graph)
    rows = [patient_row(71601), patient_row(71602)]
    source = patients_csv(0, extra=rows)
    report, _receipt = committed(world, source, monkeypatch)
    if mismatch == "none":
        result = reconcile_source(world, report.job.job_id, source, rows)
        assert result["provenance_records_matched"] == 2
        assert result["retained_source_matches"] is True
        return
    if mismatch == "bytes":
        source = source.replace(b"Legada", b"Errada")
    else:
        column, replacement = {
            "source_id": (0, "SIN-WRONG"),
            "author": (7, "Wrong synthetic author"),
            "time": (6, "06/03/2019 09:30"),
            "name": (1, "Wrong synthetic patient"),
        }[mismatch]
        rows[0][column] = replacement
    with pytest.raises(AssertionError):
        reconcile_source(world, report.job.job_id, source, rows)
