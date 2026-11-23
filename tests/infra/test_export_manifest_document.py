"""The published export format IDs match their manifest fields."""

import re
from pathlib import Path

import pytest
from apps.interop.export_registry import FORMAT

DOCS = Path(__file__).resolve().parents[2] / "docs" / "api"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (1, "clinic-organization-export-v1"),
        (2, "clinic-organization-export-v2"),
        (3, "clinic-organization-export-v3"),
        (4, "clinic-organization-export-v4"),
        (5, FORMAT),
    ],
)
def test_export_manifest_document_version(version: int, expected: str) -> None:
    document = (DOCS / f"export-v{version}.md").read_text(encoding="utf-8")
    assert re.findall(r"(?m)^\| `v` \| `([^`]+)` \|$", document) == [expected]
