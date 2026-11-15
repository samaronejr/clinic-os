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
    ("mismatch", "patient_count"),
    [
        pytest.param("none", 1, id="one-patient"),
        pytest.param("none", 2, id="none"),
        pytest.param("none", 4, id="s16-four-patients"),
        pytest.param("bytes", 2, id="bytes"),
        pytest.param("source_id", 2, id="source_id"),
        pytest.param("author", 2, id="author"),
        pytest.param("time", 2, id="time"),
        pytest.param("name", 2, id="name"),
    ],
)
def test_reconciliation_rejects_wrong_source_or_provenance(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
    patient_count: int,
) -> None:
    world = import_world(rbac_graph)
    rows = [patient_row(71601 + index) for index in range(patient_count)]
    source = patients_csv(0, extra=rows)
    report, _receipt = committed(world, source, monkeypatch)
    if mismatch == "none":
        result = reconcile_source(world, report.job.job_id, source, rows)
        assert result["provenance_records_matched"] == patient_count
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
