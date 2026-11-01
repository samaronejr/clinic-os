"""Offline benchmark reports never manufacture vendor or clinical evidence."""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.ai.benchmark import (
    OFFLINE_CANDIDATES,
    benchmark,
    markdown_report,
    parse_report,
    record_selections,
)
from apps.ai.contracts import ProviderTimeoutError, Purpose
from apps.ai.evaluation.corpus import ADVERSARIAL_TAGS
from apps.ai.evaluation.protocol import CLINICAL_LABELS, EvaluationInputError
from apps.identity.management.base import LifecycleCommandError
from apps.providers.models import CapabilityApproval, CapabilityVersion
from django.core.management import CommandError, call_command

from otp_test_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks: Iterator[int] = iter(range(0, 1_000_000_000, 1_000_000))
    monkeypatch.setattr("apps.ai.benchmark.perf_counter_ns", lambda: next(ticks))


def test_complete_report_schema_and_measured_units(fixed_clock: None) -> None:
    report = benchmark(purposes=(Purpose.SCRIBE_ASR, Purpose.ANALYST))
    parsed = parse_report(markdown_report(report))
    assert parsed["schema_version"] == 1
    assert parsed["external_gates_satisfied"] == []
    assert parsed["real_world_performance"] is False
    assert parsed["provider_leg"] == "BLOCKED-ON-EG"
    assert set(report.adversarial_classes) == ADVERSARIAL_TAGS
    for selection in report.selections:
        assert selection.primary is not None
        assert selection.fallback is not None
        for candidate in selection.candidates:
            assert candidate.samples == 12
            assert candidate.latency_ms == {"p50": 1.0, "p95": 1.0}
            assert candidate.cost_per_visit_micros == 0
            assert set(candidate.clinical_error_rates) == set(CLINICAL_LABELS)
            assert all(rate is None for rate in candidate.clinical_error_rates.values())
            assert candidate.clinical_review_status == "pending_two_raters"
    asr = report.selections[0].candidates[0]
    assert asr.usage_per_visit["audio_seconds"] == 3
    assert asr.wer["reference_words"] > 0
    assert asr.wer["errors"] > 0
    assert asr.structured_output["valid"] == 12


@pytest.mark.parametrize("field", ["region", "retention"])
def test_missing_or_unverified_terms_are_ineligible(field: str) -> None:
    original = OFFLINE_CANDIDATES[1]
    missing = replace(original, **{field: replace(getattr(original, field), value="")})
    unverified = replace(
        original,
        **{field: replace(getattr(original, field), verification="unverified")},
    )
    for candidate in (missing, unverified):
        report = benchmark(
            purposes=(Purpose.SCRIBE_ASR,),
            candidates=(OFFLINE_CANDIDATES[0], candidate),
        )
        selection = report.selections[0]
        assert selection.fallback is None
        rejected = selection.candidates[1]
        assert rejected.eligible is False
        assert f"{field}_unverified" in rejected.reasons
        assert parse_report(markdown_report(report))["selections"]


@pytest.mark.parametrize("claimed_jurisdiction", ["EU", "BR"])
def test_foreign_fallback_cannot_silently_change_jurisdiction(
    claimed_jurisdiction: str,
) -> None:
    foreign = replace(
        OFFLINE_CANDIDATES[1],
        jurisdiction=claimed_jurisdiction,
        region=replace(OFFLINE_CANDIDATES[1].region, value="eu-synthetic"),
    )
    selection = benchmark(
        purposes=(Purpose.SCRIBE_ASR,),
        candidates=(OFFLINE_CANDIDATES[0], foreign),
    ).selections[0]
    assert selection.primary == OFFLINE_CANDIDATES[0].name
    assert selection.fallback is None
    assert selection.candidates[1].reasons == ("jurisdiction_mismatch",)


def test_failures_are_counted_and_never_echo_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise ProviderTimeoutError

    monkeypatch.setattr("apps.ai.benchmark.call_provider", unavailable)
    report = benchmark(purposes=(Purpose.SCRIBE_ASR,))
    selection = report.selections[0]
    assert selection.primary is None
    assert selection.fallback is None
    for candidate in selection.candidates:
        assert candidate.failures == {"timeout": 12}
        assert candidate.structured_output["valid"] == 0
        assert candidate.wer["rate"] == 1
        assert candidate.eligible is False
    rendered = markdown_report(report)
    assert "script" not in rendered
    assert "audio_base64" not in rendered
    assert "output" not in parse_report(rendered)


def test_live_adapter_and_duplicate_identity_refused_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid candidate reached provider dispatch")

    monkeypatch.setattr("apps.ai.benchmark.call_provider", forbidden)
    for candidates in (
        (replace(OFFLINE_CANDIDATES[0], adapter="aws"),),
        (OFFLINE_CANDIDATES[0], OFFLINE_CANDIDATES[0]),
    ):
        with pytest.raises(EvaluationInputError):
            benchmark(purposes=(Purpose.SCRIBE_ASR,), candidates=candidates)


def test_frontmatter_rejects_schema_drift_and_false_external_claim() -> None:
    report = benchmark(purposes=(Purpose.SCRIBE_ASR,))
    rendered = markdown_report(report)
    metadata = parse_report(rendered)
    for key, value in (
        ("schema_version", 2),
        ("external_gates_satisfied", ["EG-1"]),
        ("real_world_performance", True),
        ("protocol_sha256", "0" * 64),
    ):
        changed = {**metadata, key: value}
        with pytest.raises(EvaluationInputError):
            parse_report("---\n" + json.dumps(changed) + "\n---\n")


def test_dated_selection_document_has_valid_machine_consumed_schema() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "docs/ai/provider-selection-2026-10-29.md"
    )
    assert parse_report(path.read_text())["data_class"] == "synthetic"


def test_cli_exports_report_without_database(tmp_path: Path) -> None:
    path = tmp_path / "bench.md"
    call_command("ai_benchmark", "--report", str(path), stdout=io.StringIO())
    report = parse_report(path.read_text())
    assert isinstance(report["selections"], list)
    assert len(report["selections"]) == len(Purpose)
    assert report["provider_leg"] == "BLOCKED-ON-EG"
    with pytest.raises(CommandError):
        call_command("ai_benchmark", "--report", str(path), stdout=io.StringIO())


def test_manual_qa_exports_region_and_jurisdiction_refusals(tmp_path: Path) -> None:
    destination = Path(os.environ.get("CLINIC_T48_EVIDENCE_ROOT", str(tmp_path)))
    missing = replace(
        OFFLINE_CANDIDATES[1],
        region=replace(OFFLINE_CANDIDATES[1].region, value=""),
    )
    foreign = replace(
        OFFLINE_CANDIDATES[1],
        jurisdiction="EU",
        region=replace(OFFLINE_CANDIDATES[1].region, value="eu-synthetic"),
    )
    for name, candidate, reason in (
        ("region-missing", missing, "region_unverified"),
        ("cross-jurisdiction", foreign, "jurisdiction_mismatch"),
    ):
        report = benchmark(
            purposes=(Purpose.SCRIBE_ASR,),
            candidates=(OFFLINE_CANDIDATES[0], candidate),
        )
        rejected = report.selections[0].candidates[1]
        assert rejected.eligible is False
        assert reason in rejected.reasons
        assert report.selections[0].fallback is None
        rendered = markdown_report(report)
        assert parse_report(rendered)
        (destination / f"{name}.md").write_text(rendered, encoding="utf-8")


@pytest.mark.django_db(transaction=True)
def test_selection_records_are_owner_only_and_never_approval(tmp_path: Path) -> None:
    report = benchmark(purposes=(Purpose.SCRIBE_ASR,))
    reference = "docs/ai/provider-selection-2026-10-29.md"
    with runtime_role(), pytest.raises(LifecycleCommandError):
        record_selections(report, record_ref=reference)
    records = record_selections(report, record_ref=reference)
    assert len(records) == 2
    assert all(row.state == CapabilityVersion.State.SELECTED_IN_PLAN for row in records)
    assert all(row.environment == "synthetic" for row in records)
    assert all(row.approval is None for row in records)
    assert {row.capability.record_ref for row in records} == {reference}
    assert not CapabilityApproval.objects.exists()
    destination = Path(os.environ.get("CLINIC_T48_EVIDENCE_ROOT", str(tmp_path)))
    receipt = [
        {
            "key": row.capability.key,
            "provider": row.provider,
            "state": row.state,
            "environment": row.environment,
            "approval": None,
            "record_ref": row.capability.record_ref,
        }
        for row in records
    ]
    (destination / "lifecycle.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )


@pytest.mark.django_db(transaction=True)
def test_cli_publication_refuses_overwrite_without_new_lifecycle_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = ("--report", "selection.md", "--record-selections")
    sink = io.StringIO()
    call_command("ai_benchmark", *arguments, stdout=sink)
    report = parse_report(Path("selection.md").read_text())
    assert report["external_gates_satisfied"] == []
    versions = CapabilityVersion.objects.count()
    assert versions > 0
    with pytest.raises(CommandError):
        call_command("ai_benchmark", *arguments, stdout=sink)
    assert CapabilityVersion.objects.count() == versions
    assert not CapabilityApproval.objects.exists()
