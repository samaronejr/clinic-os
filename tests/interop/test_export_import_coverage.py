"""Pin the committed v5 compatibility classification to the live registry."""

import re
from collections import Counter
from pathlib import Path

import pytest
from apps.interop.access import Code, ImportInputError
from apps.interop.export_import import IMPORTABLE_DATASETS, _visible_scopes
from apps.interop.export_import_archive import VerifiedExport
from apps.interop.export_registry import datasets

COVERAGE = Path(__file__).resolve().parents[2] / "docs/api/export-v5-import-coverage.md"


def test_every_v5_dataset_has_one_import_classification_and_reason() -> None:
    classified: Counter[str] = Counter()
    importable: set[str] = set()
    for line in COVERAGE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or line.startswith("| Class |"):
            continue
        _, status, relations, reason, _ = line.split("|")
        status = status.strip()
        assert status in {"IMPORTABLE", "EXPORT-ONLY"}
        names = [
            name.strip().removeprefix("`").removesuffix("`")
            for name in relations.split(",")
        ]
        assert reason.strip()
        assert all(re.fullmatch(r"[a-z][a-z0-9_]+", name) for name in names)
        classified.update(names)
        if status == "IMPORTABLE":
            importable.update(names)
    assert classified == Counter(entry.relation for entry in datasets())
    assert importable == IMPORTABLE_DATASETS


def test_v5_rejects_unresolvable_hidden_enrollment_instead_of_revealing_it() -> None:
    verified = VerifiedExport(
        rows={
            "intake_patientclinicenrollment": [
                {
                    "id": "synthetic-enrollment",
                    "patient_id": "patient",
                    "clinic_id": "clinic",
                }
            ],
            "interop_hiddenenrollment": [{"enrollment_id": "missing-enrollment"}],
        },
        attachments={},
    )
    with pytest.raises(ImportInputError) as refused:
        _visible_scopes(verified)
    assert refused.value.code == Code.MANIFEST_MISMATCH
