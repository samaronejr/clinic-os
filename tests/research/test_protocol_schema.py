from __future__ import annotations

import hashlib
import json
import math
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTOCOLS = (
    ROOT / "docs/research/usability-protocol.md",
    ROOT / "docs/research/scribe-study-protocol.md",
)
RESULTS_TEMPLATE = ROOT / "docs/research/results-template.md"
CAPTURE_FORMS = ROOT / "docs/operations/research-capture-forms.md"
FENCE = "+++\n"
PREREG_BEGIN = "<!-- prereg:begin -->"
PREREG_END = "<!-- prereg:end -->"
TARGET_FIELDS = frozenset(
    {"id", "label", "metric", "comparator", "value", "numerator", "denominator"}
)
STATES = frozenset({"draft", "pre-registered"})
DENIED_FORM_FIELDS = frozenset(
    {"name", "email", "phone", "cpf", "birth_date", "date_of_birth", "notes", "quote"}
)

SCRIBE_SECONDARY_FIELDS = frozenset(
    {
        "omission_count",
        "hallucination_count",
        "medication_name_dose_error_count",
        "allergy_error_count",
        "negation_error_count",
        "correction_count",
        "nasa_tlx_raw_score",
    }
)


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(FENCE):
        msg = "missing front matter"
        raise ValueError(msg)
    head, _, body = text[len(FENCE) :].partition(FENCE)
    return tomllib.loads(head), body


def prereg_digest(text: str) -> str:
    """Hash the registered machine fields together with the prereg block."""
    meta, body = split_front_matter(text)
    registered = {key: value for key, value in meta.items() if key != "prereg_sha256"}
    start = body.index(PREREG_BEGIN)
    end = body.index(PREREG_END, start) + len(PREREG_END)
    canonical = json.dumps(registered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{canonical}\n{body[start:end]}".encode()).hexdigest()


def protocol_problems(text: str) -> list[str]:
    meta, _ = split_front_matter(text)
    problems: list[str] = []
    if meta.get("state") not in STATES:
        problems.append("state")
    if meta.get("gate_status") != "BLOCKED-ON-EG":
        problems.append("gate_status")
    targets = meta.get("targets") or []
    if not targets:
        problems.append("targets")
    for target in targets:
        missing = TARGET_FIELDS - {k for k, v in target.items() if v not in ("", None)}
        problems.extend(
            f"target {target.get('id')}: {field}" for field in sorted(missing)
        )
        if target.get("label") != "proposal":
            problems.append(f"target {target.get('id')}: label")
    sample = meta.get("sample_size") or {}
    if sample.get("label") != "proposal" or not sample.get("unit"):
        problems.append("sample_size")
    if meta.get("state") == "pre-registered" and meta.get(
        "prereg_sha256"
    ) != prereg_digest(text):
        problems.append("prereg_sha256 mismatch")
    return problems


@pytest.mark.parametrize("path", PROTOCOLS, ids=lambda p: p.name)
def test_protocol_front_matter_is_valid(path: Path) -> None:
    assert protocol_problems(path.read_text()) == []


@pytest.mark.parametrize("path", PROTOCOLS, ids=lambda p: p.name)
def test_missing_denominator_fails(path: Path) -> None:
    text = path.read_text()
    meta, _ = split_front_matter(text)
    first = meta["targets"][0]["denominator"]
    broken = text.replace(f'denominator = "{first}"', 'denominator = ""', 1)
    assert f"target {meta['targets'][0]['id']}: denominator" in protocol_problems(
        broken
    )


@pytest.mark.parametrize("path", PROTOCOLS, ids=lambda p: p.name)
def test_prereg_block_edit_is_flagged(path: Path) -> None:
    text = path.read_text()
    edited = text.replace(PREREG_END, f"edited outcome\n{PREREG_END}", 1)
    assert prereg_digest(edited) != prereg_digest(text)
    assert "prereg_sha256 mismatch" in protocol_problems(edited)


@pytest.mark.parametrize("path", PROTOCOLS, ids=lambda p: p.name)
@pytest.mark.parametrize("field", ["metric", "value", "numerator", "denominator"])
def test_machine_outcome_edit_is_flagged(path: Path, field: str) -> None:
    text = path.read_text()
    meta, _ = split_front_matter(text)
    target = meta["targets"][0]
    current = target[field]
    if isinstance(current, str):
        old, new = f'{field} = "{current}"', f'{field} = "{current} (edited)"'
    else:
        old, new = f"{field} = {current}", f"{field} = {current + 0.05}"
    assert old in text
    edited = text.replace(old, new, 1)
    assert prereg_digest(edited) != prereg_digest(text)
    assert "prereg_sha256 mismatch" in protocol_problems(edited)


def test_sample_size_assumption_edit_is_flagged() -> None:
    text = PROTOCOLS[1].read_text()
    edited = text.replace("enrollment_target = 22", "enrollment_target = 12", 1)
    assert edited != text
    assert "prereg_sha256 mismatch" in protocol_problems(edited)


def test_scribe_sample_size_matches_its_assumptions() -> None:
    meta, _ = split_front_matter(PROTOCOLS[1].read_text())
    s = meta["sample_size"]
    delta = (
        s["baseline_median_minutes_assumption"] * s["target_median_reduction_fraction"]
    )
    sd = s["sd_paired_difference_minutes_assumption"]
    paired = ((s["z_alpha"] + s["z_beta"]) * sd / delta) ** 2
    required = math.ceil(paired / s["wilcoxon_are_assumption"])
    assert s["required_completers"] == required
    assert s["enrollment_target"] == math.ceil(required / (1 - s["dropout_assumption"]))


def test_scribe_primary_target_is_median_based() -> None:
    meta, _ = split_front_matter(PROTOCOLS[1].read_text())
    primary = meta["targets"][0]
    assert primary["value"] == 0.30
    assert "median" in primary["metric"]
    assert primary["test"].startswith("Wilcoxon signed-rank")
    assert primary["estimator"].startswith("Hodges-Lehmann")


def test_usability_sample_size_has_strata() -> None:
    meta, _ = split_front_matter(PROTOCOLS[0].read_text())
    s = meta["sample_size"]
    assert s["per_stratum"] > 0
    assert s["roles"]
    assert s["devices"]


def test_results_template_requires_denominators() -> None:
    meta, body = split_front_matter(RESULTS_TEMPLATE.read_text())
    fields = meta["required_row_fields"]
    assert {"numerator", "denominator", "prereg_sha256", "session_ids"} <= set(fields)
    header = next(
        line for line in body.splitlines() if line.startswith("| protocol_id")
    )
    assert [cell.strip() for cell in header.strip("|").split("|")] == fields


def test_capture_forms_hold_session_ids_only() -> None:
    meta, _ = split_front_matter(CAPTURE_FORMS.read_text())
    protocol_ids = {
        split_front_matter(p.read_text())[0]["protocol_id"] for p in PROTOCOLS
    }
    assert {form["protocol_id"] for form in meta["forms"]} == protocol_ids
    for form in meta["forms"]:
        assert form["fields"][0] == "session_id"
        assert not DENIED_FORM_FIELDS & set(form["fields"])
    scribe_fields = {
        field
        for form in meta["forms"]
        if form["protocol_id"] == "scribe-effort-v1"
        for field in form["fields"]
    }
    assert scribe_fields >= SCRIBE_SECONDARY_FIELDS
