"""Report schema for the ADR-013 browser capability study.

One engine report is written by the ``device-capabilities`` suite; the study
report in ADR-013 merges the engine reports. Both carry the evidence level:
emulation is L1, and real-device proof stays ``pending-EG-14`` because only an
owner-supplied device run under EG-14 can supply it.
"""

from __future__ import annotations

from typing import Final, Never

CRITERIA: Final = ("background-audio", "push", "camera-capture", "webauthn-platform")
ENGINES: Final = ("chromium", "firefox", "webkit")
SUBCHECK_RESULTS: Final = ("pass", "fail", "unmeasurable")
CRITERION_STATUSES: Final = ("pass", "fail")
EVIDENCE_LEVEL: Final = "L1-emulation"
DEVICE_PROOF: Final = "pending-EG-14"


class ReportError(ValueError):
    """A capability report breaks the study schema."""


def _fail(message: str) -> Never:
    raise ReportError(message)


def criterion_status(subchecks: list[dict[str, str]]) -> str:
    """``fail`` if a subcheck failed, else ``pass``; unmeasurable ones are listed."""
    return "fail" if any(item["result"] == "fail" for item in subchecks) else "pass"


def validate_engine_report(report: dict[str, object]) -> None:
    """Check one engine report: closed criteria, labelled L1, no device claim."""
    if (
        report.get("schema_version") != 1
        or report.get("suite") != "device-capabilities"
    ):
        _fail("engine report must be schema_version 1 of the device-capabilities suite")
    if report.get("engine") not in ENGINES:
        _fail(f"unknown engine {report.get('engine')!r}")
    if report.get("evidence_level") != EVIDENCE_LEVEL:
        _fail(f"evidence_level must be {EVIDENCE_LEVEL}")
    if report.get("device_proof") != DEVICE_PROOF:
        _fail(f"device_proof must be {DEVICE_PROOF}")
    criteria = report.get("criteria")
    if not isinstance(criteria, list) or [c.get("id") for c in criteria] != list(
        CRITERIA
    ):
        _fail(f"criteria must be exactly {list(CRITERIA)} in order")
    for criterion in criteria:
        subchecks = criterion.get("subchecks")
        if not subchecks or any(
            item.get("result") not in SUBCHECK_RESULTS or not item.get("name")
            for item in subchecks
        ):
            _fail(f"{criterion['id']}: every subcheck needs a name and a closed result")
        if criterion.get("status") != criterion_status(subchecks):
            _fail(f"{criterion['id']}: status disagrees with its subchecks")


def _validate_study_criteria(criteria: object) -> None:
    if not isinstance(criteria, list) or [c.get("id") for c in criteria] != list(
        CRITERIA
    ):
        _fail(f"criteria must be exactly {list(CRITERIA)} in order")
    for criterion in criteria:
        by_engine = criterion.get("l1_by_engine")
        if not isinstance(by_engine, dict) or list(by_engine) != list(ENGINES):
            _fail(f"{criterion['id']}: l1_by_engine must cover {list(ENGINES)}")
        if any(value not in CRITERION_STATUSES for value in by_engine.values()):
            _fail(f"{criterion['id']}: engine status must be pass or fail")
        expected = "fail" if "fail" in by_engine.values() else "pass"
        if criterion.get("l1_status") != expected:
            _fail(f"{criterion['id']}: l1_status disagrees with the engines")
        if criterion.get("device_status") != DEVICE_PROOF:
            _fail(f"{criterion['id']}: device_status must be {DEVICE_PROOF}")
        if not criterion.get("consequence"):
            _fail(f"{criterion['id']}: record the consequence")


def validate_study(study: dict[str, object]) -> None:
    """Check the merged study report recorded in ADR-013."""
    if study.get("schema_version") != 1 or study.get("study") != "ADR-013":
        _fail("study report must be schema_version 1 of ADR-013")
    if study.get("evidence_level") != EVIDENCE_LEVEL:
        _fail(f"evidence_level must be {EVIDENCE_LEVEL}")
    if study.get("device_proof") != DEVICE_PROOF:
        _fail(f"device_proof must be {DEVICE_PROOF}")
    if study.get("engines") != list(ENGINES):
        _fail(f"engines must be {list(ENGINES)}")
    _validate_study_criteria(study.get("criteria"))
    if not isinstance(study.get("decision"), dict):
        _fail("study report needs a decision object")
