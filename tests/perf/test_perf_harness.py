"""Todo 71: load profiles, SLO reports and fairness stress.

Pure helpers run everywhere. The scenario tests seed a tiny synthetic profile
and run the real services as ``clinic_app`` on PostgreSQL; the Redis-backed
checks (realtime fan-out, fairness) run under the dedicated broker gate like the
other fairness suites.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import random
import re
import subprocess
import sys
import time
from contextlib import nullcontext
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from apps.core import telemetry
from ops.perf import dataset, fairness_runs, report, scenarios
from ops.perf.latency_model import (
    ModelParams,
    draft_generation_ms,
    transcript_partial_ms,
)
from ops.perf.profiles import (
    NOT_MEASURED,
    PROFILES,
    TARGETS,
    UnknownProfileError,
    profile_named,
    target_for,
)
from ops.perf.report import ReportError, ScenarioResult
from ops.perf.run import SLOW_MARKERS, main, run_profile
from ops.perf.runtime import slow_queries
from ops.perf.stats import StatsInputError, percentile, summarize
from ops.perf.threads import WorkerResult

from core.test_fairness import fairness_broker, metered_broker

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

__all__ = ["fairness_broker", "metered_broker"]

ROOT = Path(__file__).resolve().parents[2]
SLO_DOC = ROOT / "docs" / "operations" / "slo.md"
GATED = pytest.mark.skipif(
    os.environ.get("CLINIC_BROKER_GATE", "") != "required",
    reason="requires the dedicated broker gate (CLINIC_BROKER_GATE=required)",
)
TINY = dataclasses.replace(
    PROFILES["P-small"],
    organizations=2,
    clinicians_per_clinic=2,
    visits_per_clinician_day=3,
    patients_per_org=40,
    staff_sessions=2,
    samples=6,
)


def _valid_report() -> dict[str, Any]:
    records = [
        report.scenario_record(ScenarioResult("autosave", "measured", [10.0, 20.0])),
    ]
    return report.build_report(
        profile=PROFILES["P-small"],
        dataset={"organizations": 1, "clinics": 1, "patients": 1, "seed": 1},
        commit={"sha": "a" * 40, "dirty": False},
        scenarios=records,
        fairness=[],
    )


def test_percentiles_are_nearest_rank() -> None:
    samples = [float(value) for value in range(1, 101)]
    summary = summarize(samples)
    assert (summary.p50, summary.p95, summary.p99, summary.maximum) == (
        50.0,
        95.0,
        99.0,
        100.0,
    )
    assert percentile([7.0], 0.99) == 7.0
    for bad in ([], [float("nan")], [-1.0]):
        with pytest.raises(StatsInputError):
            percentile(bad, 0.5)
    with pytest.raises(StatsInputError):
        percentile([1.0], 0.0)


def test_scenario_status_follows_the_pf_target() -> None:
    limit = target_for("autosave")
    assert limit is not None
    assert limit.limit_ms == 1000

    def status(result: ScenarioResult) -> str:
        return str(report.scenario_record(result)["status"])

    within = [100.0] * 20
    over = [100.0] * 18 + [1500.0] * 2
    assert status(ScenarioResult("autosave", "measured", within)) == "pass"
    assert status(ScenarioResult("autosave", "measured", over)) == "fail"
    assert status(ScenarioResult("autosave", "measured", within, errors=1)) == "fail"
    assert status(ScenarioResult("autosave", "measured", [])) == "fail"
    # No PF target: informational, never pass or fail.
    assert status(ScenarioResult("chunk_upload", "component", over)) == "informational"
    # A model never counts as a target met, even when it is under the limit.
    assert (
        status(ScenarioResult("draft_generation", "modeled", [1.0])) == "informational"
    )


def test_fairness_verdict_uses_the_ten_percent_tolerance() -> None:
    def verdict(loaded: float, *, also_required: bool = True) -> str:
        return str(
            report.fairness_record(
                fairness_id="x",
                baseline_p95_ms=100.0,
                loaded_p95_ms=loaded,
                trials=1,
                load={},
                also_required=also_required,
            )["status"]
        )

    assert verdict(110.0) == "pass"
    assert verdict(110.5) == "fail"
    assert verdict(90.0) == "pass"
    assert verdict(100.0, also_required=False) == "fail"


def test_registry_search_remains_measured_without_an_slo() -> None:
    result = report.scenario_record(
        ScenarioResult("patient_search_registry", "measured", [61_784.970])
    )
    assert target_for("patient_search_registry") is None
    assert result["kind"] == "measured"
    assert result["samples"] == 1
    assert result["p95"] == 61_784.970
    assert result["target"] is None
    assert result["status"] == "informational"
    text = SLO_DOC.read_text(encoding="utf-8")
    assert "2026-11-12" in text
    assert "Clinic Ops owner" in text
    assert "Search-options.md" in text
    assert "no SLO assigned" in text
    assert "substring registry needs its own owner-approved performance" in text


def test_fairness_rejects_latencies_from_failed_autosaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = scenarios.Prepared(
        dataset=dataset.Dataset(profile=PROFILES["P-small"], seed=1, tenants=[])
    )
    monkeypatch.setattr(
        fairness_runs,
        "autosave_run",
        lambda *_args, **_kwargs: WorkerResult(samples_ms=[1.0], errors=1),
    )
    with pytest.raises(
        RuntimeError, match="autosave fairness sample contains failed saves"
    ):
        fairness_runs._p95(prepared, 1)


def test_import_fairness_rejects_work_completed_only_before_the_saves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = scenarios.Prepared(
        dataset=dataset.Dataset(profile=PROFILES["P-small"], seed=1, tenants=[])
    )
    monkeypatch.setattr(
        fairness_runs,
        "_alternate",
        lambda *_args, **_kwargs: (
            100.0,
            100.0,
            {
                "during": {"import_rows_written_so_far": 1},
                "trials_ms": [{"work_completed_during_saves": 0}],
            },
        ),
    )
    assert fairness_runs.chart_save_vs_import(prepared, trials=1)["status"] == "fail"


def test_fairness_windows_exclude_warmup_and_victim_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = scenarios.Prepared(
        dataset=dataset.Dataset(profile=PROFILES["P-small"], seed=1, tenants=[])
    )
    load = Mock(first=100, second=200)
    clock = iter([0.0, 2.0, 10.0, 14.0, 20.0, 23.0, 30.0, 35.0])
    samples = iter([999.0, 100.0, 110.0, 200.0, 220.0])

    def sample(*_args: object) -> float:
        value = next(samples)
        if value in {110.0, 220.0}:
            load.first += 3
            load.second += 17
        return value

    def probes(current: Mock) -> dict[str, int]:
        current.first += 1000
        current.second += 2000
        return {"victim_probes": 40}

    monkeypatch.setattr(fairness_runs, "_saves", lambda _prepared: 25)
    monkeypatch.setattr(fairness_runs, "_p95", sample)
    monkeypatch.setattr(time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(fairness_runs, "resource_snapshot", dict)
    baseline, loaded, detail = fairness_runs._alternate(
        prepared, 2, lambda: nullcontext(load), probes
    )
    assert (baseline, loaded) == (150.0, 165.0)
    for trial, base_seconds, loaded_seconds in zip(
        detail["trials_ms"], [2.0, 3.0], [4.0, 5.0], strict=True
    ):
        assert trial["baseline_window_seconds"] == base_seconds
        assert trial["loaded_window_seconds"] == loaded_seconds
        assert trial["work_completed_during_saves"] == 3
        assert trial["work_deferred_during_saves"] == 17
    assert detail["during"]["victim_probes"] == 40


def test_broker_cleanup_refuses_unscoped_queues() -> None:
    from ops.perf.broker import clear_import_queue  # noqa: PLC0415

    with pytest.raises(ValueError, match="not a scoped performance import queue"):
        clear_import_queue("bulk")


def test_report_schema_accepts_a_valid_report_and_rejects_drift() -> None:
    valid = _valid_report()
    import_module("jsonschema").Draft202012Validator.check_schema(
        json.loads(report.SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    report.validate_report(valid)
    assert valid["evidence_level"] == "L1"
    assert [item["target"] for item in valid["not_measured"]] == [
        item[0] for item in NOT_MEASURED
    ]
    broken: list[dict[str, Any]] = []
    for key, value in (
        ("evidence_level", "L2"),
        ("profile", "P-unknown"),
        ("commit", {"sha": "short", "dirty": False}),
        ("surprise", 1),
    ):
        candidate = json.loads(json.dumps(valid))
        candidate[key] = value
        broken.append(candidate)
    missing = json.loads(json.dumps(valid))
    del missing["commit"]
    broken.append(missing)
    silent_pass = json.loads(json.dumps(valid))
    silent_pass["scenarios"][0].update(samples=0, status="pass")
    broken.append(silent_pass)
    for candidate in broken:
        with pytest.raises(ReportError):
            report.validate_report(candidate)


def test_profiles_carry_the_plan_shapes_and_targets() -> None:
    assert PROFILES["P-large-tenant"].patients_per_org == 100_000
    assert PROFILES["P-large-tenant"].staff_sessions == 50
    assert PROFILES["P-large-tenant"].bulk_import
    hundred = PROFILES["P-100"]
    assert (
        hundred.organizations
        * hundred.clinicians_per_clinic
        * hundred.visits_per_clinician_day
        == 25_000
    )
    assert not PROFILES["P-1000"].runnable_locally
    assert {item.scenario: item.limit_ms for item in TARGETS} == {
        "staff_navigation": 1500,
        "patient_search_blind_index": 700,
        "agenda_interaction": 700,
        "autosave": 1000,
        "realtime_fanout": 2000,
        "transcript_partial": 3000,
        "draft_generation": 30_000,
    }
    with pytest.raises(UnknownProfileError):
        profile_named("P-nope")


def test_cli_refuses_unrunnable_profiles_and_shared_databases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["--profile", "P-1000"]) == 2
    assert "definition only" in capsys.readouterr().err
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", "postgresql://clinic_owner@localhost:5432/clinic"
    )
    assert main(["--profile", "P-small", "--run-root", str(tmp_path)]) == 2
    assert "throwaway" in capsys.readouterr().err


def test_latency_model_is_seeded_and_deterministic() -> None:
    params = ModelParams()

    def draw(seed: int) -> list[float]:
        rng = random.Random(seed)  # noqa: S311 - reproducible model draw
        return [
            transcript_partial_ms(params=params, ingest_ms=2.0, rng=rng)
            for _ in range(5)
        ] + [draft_generation_ms(params=params, persist_ms=2.0, rng=rng)]

    assert draw(1) == draw(1)
    assert draw(1) != draw(2)
    assert all(value > 0 for value in draw(3))


def test_slo_doc_slis_use_real_metrics_routes_and_targets() -> None:
    text = SLO_DOC.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (SLO-\d+) \|.*\| `(\w+)` \|", text, flags=re.MULTILINE)
    documented = {scenario for _, scenario in rows}
    assert {item.scenario for item in TARGETS} <= documented
    telemetry_source = Path(telemetry.__file__).read_text(encoding="utf-8")
    metrics = set(re.findall(r"\bclinic_[a-z_]+", text))
    base_names = {re.sub(r"_(bucket|sum|count)$", "", name) for name in metrics}
    assert base_names
    for name in base_names:
        assert name in telemetry_source, name
    routes = set(re.findall(r'route="([^"]+)"', text))
    assert routes
    assert routes <= telemetry._route_names()
    purposes = set(re.findall(r'purpose="([^"]+)"', text))
    assert purposes <= set(telemetry.AI_INVOCATION_PURPOSES)
    assert "ops.perf.run --profile P-large-tenant" in text


def test_locust_file_defines_staff_tasks_and_a_correct_totp() -> None:
    path = ROOT / "ops" / "perf" / "locustfile.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tasks = [
        node.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "StaffUser"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.decorator_list
    ]
    assert tasks == ["workspace", "agenda_board", "patient_list"]
    # RFC 6238 appendix B, SHA-1 secret "12345678901234567890" at T=59:
    # 94287082, so the six-digit code is 287082.
    probe = (
        "from ops.perf.locustfile import totp; "
        "print(totp(b'12345678901234567890'.hex(), 59))"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert done.stdout.strip() == "287082"


@pytest.mark.django_db(transaction=True)
def test_tiny_profile_runs_the_real_services_as_clinic_app(
    synthetic_secret_backend: Path,
) -> None:
    world = dataset.seed_dataset(TINY, seed=7)
    assert world.patients == 80
    assert world.focus.patient_count == 40
    assert len(world.focus.sample_names) == 40
    prepared = scenarios.prepare(world, editors=2)
    assert prepared.booked == 6
    runs = {
        "patient_search_blind_index": scenarios.patient_search_blind_index(prepared),
        "patient_search_registry": scenarios.patient_search_registry(prepared),
        "staff_navigation": scenarios.staff_navigation(prepared),
        "agenda_interaction": scenarios.agenda_interaction(prepared),
        "autosave": scenarios.autosave(prepared),
        "chunk_upload": scenarios.chunk_upload(prepared),
    }
    for name, result in runs.items():
        assert result.scenario_id == name
        assert result.errors == 0, name
        assert result.samples_ms, name
    assert runs["chunk_upload"].kind == "measured"
    from apps.scribe.models import AudioChunk, RecordingSession  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from ops.perf.runtime import runtime_role  # noqa: PLC0415

    with (
        runtime_role(),
        tenant_context(world.focus.physician_ids[0], world.focus.organization_id),
    ):
        chunks = list(AudioChunk.objects.all())
        assert len(chunks) == TINY.samples
        assert all(chunk.operation_id and chunk.encrypted_data for chunk in chunks)
        assert len({chunk.epoch_id for chunk in chunks}) == TINY.samples
        assert all(chunk.seq == 0 for chunk in chunks)
        assert RecordingSession.objects.get().state == "stopped"
    assert len(runs["autosave"].samples_ms) == 6
    # Every autosave advanced the draft: each editor saved ``saves`` times.
    assert all(draft.revision > 1 for draft in prepared.drafts)
    revisions = [draft.revision for draft in prepared.drafts]
    repeated = scenarios.autosave(prepared)
    assert repeated.errors == 0
    assert all(
        draft.revision > previous
        for draft, previous in zip(prepared.drafts, revisions, strict=True)
    )
    modeled = scenarios.transcript_partial(prepared, runs["chunk_upload"])
    assert modeled.kind == "modeled"
    assert report.scenario_record(modeled)["status"] == "informational"


@pytest.mark.django_db(transaction=True)
def test_injected_slow_query_is_recorded_as_a_failed_target(
    synthetic_secret_backend: Path,
) -> None:
    profile = dataclasses.replace(TINY, organizations=1, samples=3)
    prepared = scenarios.prepare(dataset.seed_dataset(profile, seed=8), editors=1)
    healthy = report.scenario_record(scenarios.patient_search_blind_index(prepared))
    assert healthy["status"] == "pass"
    with slow_queries(400, SLOW_MARKERS):
        slowed = report.scenario_record(scenarios.patient_search_blind_index(prepared))
    # Each search runs two matching statements, so every sample takes >= 800 ms.
    assert slowed["p95"] > 700
    assert slowed["status"] == "fail"
    assert slowed["errors"] == 0


@GATED
@pytest.mark.django_db(transaction=True)
def test_full_run_reports_every_scenario_and_fairness(
    synthetic_secret_backend: Path,
    metered_broker: str,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    realtime_url = metered_broker.rsplit("/", 1)[0] + "/1"
    monkeypatch.setenv("CELERY_BROKER_URL", metered_broker)
    monkeypatch.setenv("REALTIME_REDIS_URL", realtime_url)
    monkeypatch.setenv("REALTIME_ENABLED", "true")
    settings.CELERY_BROKER_URL = metered_broker
    settings.REALTIME_REDIS_URL = realtime_url
    settings.REALTIME_ENABLED = True
    profile = dataclasses.replace(TINY, bulk_import=True, samples=12)
    result = run_profile(profile, seed=9, only=None, fairness_trials=1)
    ids = {item["id"] for item in result["scenarios"]}
    assert ids == {
        "patient_search_blind_index",
        "patient_search_registry",
        "staff_navigation",
        "agenda_interaction",
        "autosave",
        "realtime_fanout",
        "chunk_upload",
        "transcript_partial",
        "draft_generation",
    }
    assert {item["id"] for item in result["fairness"]} == {
        "chart_save_during_bulk_import",
        "clinical_latency_during_ai_batch_flood",
    }
    imported = next(
        item
        for item in result["fairness"]
        if item["id"] == "chart_save_during_bulk_import"
    )["load"]["during"]
    assert imported["entrypoint"] == "apps.interop.tasks.commit_batch"
    assert imported["import_source_rows"] == 10_000
    assert imported["import_batch_rows"] == 1
    assert imported["import_rows_written_so_far"] > 0
    assert imported["work_completed_during_saves"] > 0
    flood = next(
        item
        for item in result["fairness"]
        if item["id"] == "clinical_latency_during_ai_batch_flood"
    )
    during = flood["load"]["during"]
    trial = flood["load"]["trials_ms"][0]
    assert trial["baseline_window_seconds"] > 0
    assert trial["loaded_window_seconds"] > 0
    assert (
        trial["completed_response_rate_per_second"]
        == (trial["admitted"] + trial["deferred"]) / trial["loaded_window_seconds"]
    )
    # The flooder is throttled by its own bucket while the victim is admitted.
    assert during["flooder_deferred"] > 0
    assert during["victim_ai_batch_admitted"] == during["victim_ai_batch_requested"]
    assert result["search_benchmark"]["revisit_trigger"].startswith("not_evaluated")
    assert result["environment"]["broker_memory"]["used_memory_peak_bytes"] > 0
    assert (
        result["commit"]["sha"]
        == subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    report.validate_report(result)
