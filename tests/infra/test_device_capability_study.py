"""ADR-013 carries a machine-checked study report (todo 69)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Final

import pytest

from renewal.browser.device_capability_report import (
    ReportError,
    validate_engine_report,
    validate_study,
)

ADR: Final = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "adr"
    / "ADR-013-offline-and-native.md"
)
BLOCK: Final = re.compile(r"```json study-report\n(?P<body>.*?)\n```", re.DOTALL)


def _study() -> dict[str, object]:
    match = BLOCK.search(ADR.read_text(encoding="utf-8"))
    assert match is not None, "ADR-013 must hold a json study-report block"
    study: dict[str, object] = json.loads(match["body"])
    return study


def test_adr_013_study_report_matches_the_schema() -> None:
    validate_study(_study())


def test_study_never_claims_device_proof() -> None:
    study = _study()
    criteria = study["criteria"]
    assert isinstance(criteria, list)
    assert {item["device_status"] for item in criteria} == {"pending-EG-14"}
    assert study["device_proof"] == "pending-EG-14"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda study: study.update(device_proof="pass"),
        lambda study: study.update(evidence_level="EG-14"),
        lambda study: study["criteria"][0].update(device_status="pass"),
        lambda study: study["criteria"][1]["l1_by_engine"].update(webkit="maybe"),
        lambda study: study["criteria"][2].update(consequence=""),
        lambda study: study["criteria"].pop(),
    ],
)
def test_study_schema_rejects_overclaims(mutate: object) -> None:
    study = copy.deepcopy(_study())
    assert callable(mutate)
    mutate(study)
    with pytest.raises(ReportError):
        validate_study(study)


def test_engine_report_rejects_status_that_hides_a_failed_subcheck() -> None:
    names = ("background-audio", "push", "camera-capture", "webauthn-platform")
    report: dict[str, object] = {
        "schema_version": 1,
        "suite": "device-capabilities",
        "engine": "chromium",
        "evidence_level": "L1-emulation",
        "device_proof": "pending-EG-14",
        "criteria": [
            {
                "id": name,
                "status": "pass",
                "subchecks": [{"name": "x", "result": "pass", "detail": ""}],
            }
            for name in names
        ],
    }
    validate_engine_report(report)
    criteria = report["criteria"]
    assert isinstance(criteria, list)
    criteria[0]["subchecks"][0]["result"] = "fail"
    with pytest.raises(ReportError):
        validate_engine_report(report)
