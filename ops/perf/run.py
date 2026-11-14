"""Run one load profile and write a schema-valid perf report.

    uv run python -m ops.perf.run --profile P-large-tenant

Needs a private throwaway PostgreSQL (``MIGRATION_DATABASE_URL``, not port 5432)
and Redis (``CELERY_BROKER_URL`` and ``REALTIME_REDIS_URL``). The output is L1
evidence on one workstation and never a production SLO claim.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ops.perf.profiles import (
    DEFAULT_FAIRNESS_TRIALS,
    PROFILES,
    Profile,
    ProfileNotRunnableError,
    UnknownProfileError,
    profile_named,
)
from ops.perf.report import (
    ScenarioResult,
    build_report,
    commit_identity,
    scenario_record,
    write_report,
)
from ops.perf.rig import ResourceRig, resource_snapshot
from ops.perf.runtime import PerfEnvironmentError, bootstrap_django

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED = 20261111
EDITORS = 12
SLOW_MARKERS = ("full_name_index", "patient_registry_count")


def _environment(injected_ms: int) -> dict[str, Any]:
    from django.db import connection  # noqa: PLC0415

    from ops.perf.broker import broker_memory  # noqa: PLC0415

    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        row = cursor.fetchone()
    environment: dict[str, Any] = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "postgres": row[0] if row else "unknown",
        "broker_memory": broker_memory(),
        "load_location": (
            "shared PostgreSQL/Redis; fixed 2 CPU/4 GiB application parent; "
            "clinical/API 1 CPU/3 GiB; AI/bulk 1 CPU/1 GiB; "
            "external producers 2 CPU/1 GiB outside the application parent"
        ),
        "resource_rig": {
            "owner_decision_date": "2026-11-13",
            "kind": "production-isolated",
            "roles": resource_snapshot(),
        },
    }
    if injected_ms:
        environment["injected_fault"] = {
            "kind": "slow_patient_query",
            "milliseconds": injected_ms,
        }
    return environment


def _search_benchmark(
    focus_patients: int, blind: dict[str, Any], registry: dict[str, Any]
) -> dict[str, Any]:
    evaluated = focus_patients >= 100_000  # noqa: PLR2004 - ADR-015 dataset size
    limit = blind["target"]["limit_ms"] if blind["target"] else 700
    triggered = evaluated and blind["p95"] is not None and blind["p95"] > limit
    if not evaluated:
        verdict = "not_evaluated_dataset_below_100000_patients"
    else:
        verdict = "triggered" if triggered else "not_triggered"
    return {
        "adr": "ADR-015",
        "patients": focus_patients,
        "blind_index_p95_ms": blind["p95"],
        "registry_substring_p95_ms": registry["p95"],
        "limit_ms": limit,
        "revisit_trigger": verdict,
    }


def _scenario_results(
    prepared: Any,  # noqa: ANN401 - ops.perf.scenarios.Prepared, imported lazily
    only: Sequence[str] | None,
    inject_slow_query_ms: int,
) -> list[ScenarioResult]:
    from ops.perf import scenarios  # noqa: PLC0415
    from ops.perf.runtime import slow_queries  # noqa: PLC0415

    def wanted(name: str) -> bool:
        return only is None or name in only

    results: list[ScenarioResult] = []
    with slow_queries(inject_slow_query_ms, SLOW_MARKERS):
        if wanted("patient_search_blind_index"):
            results.append(scenarios.patient_search_blind_index(prepared))
        if wanted("patient_search_registry"):
            results.append(scenarios.patient_search_registry(prepared))
    for name, runner in (
        ("staff_navigation", scenarios.staff_navigation),
        ("agenda_interaction", scenarios.agenda_interaction),
        ("autosave", scenarios.autosave),
        ("realtime_fanout", scenarios.realtime_fanout),
    ):
        if wanted(name):
            results.append(runner(prepared))
    chunk = scenarios.chunk_upload(prepared)
    if wanted("chunk_upload"):
        results.append(chunk)
    if wanted("transcript_partial"):
        results.append(scenarios.transcript_partial(prepared, chunk))
    if wanted("draft_generation"):
        results.append(scenarios.draft_generation(prepared, chunk))
    return results


def run_profile(
    profile: Profile,
    *,
    seed: int = DEFAULT_SEED,
    only: Sequence[str] | None = None,
    inject_slow_query_ms: int = 0,
    fairness_trials: int = DEFAULT_FAIRNESS_TRIALS,
) -> dict[str, Any]:
    """Seed the profile, run its scenarios and return the validated report.

    Django must already be configured (see ``bootstrap_django``).
    """
    from ops.perf import fairness_runs, scenarios  # noqa: PLC0415
    from ops.perf.dataset import seed_dataset  # noqa: PLC0415

    with ResourceRig():
        dataset = seed_dataset(profile, seed=seed)
        editors = min(EDITORS, profile.clinicians_per_clinic)
        prepared = scenarios.prepare(dataset, editors=editors)
        records = [
            scenario_record(item)
            for item in _scenario_results(prepared, only, inject_slow_query_ms)
        ]
        fairness: list[dict[str, Any]] = []
        if only is None or "fairness" in only:
            if profile.bulk_import:
                fairness.append(
                    fairness_runs.chart_save_vs_import(prepared, trials=fairness_trials)
                )
            fairness.append(
                fairness_runs.ai_batch_flood(prepared, trials=fairness_trials)
            )
        by_id = {item["id"]: item for item in records}
        benchmark = None
        if "patient_search_blind_index" in by_id and "patient_search_registry" in by_id:
            benchmark = _search_benchmark(
                dataset.focus.patient_count,
                by_id["patient_search_blind_index"],
                by_id["patient_search_registry"],
            )
        return build_report(
            profile=profile,
            dataset={
                "organizations": len(dataset.tenants),
                "clinics": len(dataset.tenants) * profile.clinics_per_org,
                "patients": dataset.patients,
                "focus_tenant_patients": dataset.focus.patient_count,
                "seed": seed,
            },
            commit=commit_identity(ROOT),
            scenarios=records,
            fairness=fairness,
            search_benchmark=benchmark,
            environment=_environment(inject_slow_query_ms),
        )


def _runnable(name: str) -> Profile:
    profile = profile_named(name)
    if not profile.runnable_locally:
        message = f"{profile.name} is a definition only and needs a multi-node rig"
        raise ProfileNotRunnableError(message)
    return profile


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the profile and write the report."""
    parser = argparse.ArgumentParser(prog="python -m ops.perf.run")
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--only", help="comma-separated scenario ids or 'fairness'")
    parser.add_argument(
        "--inject-slow-query-ms",
        type=int,
        default=0,
        help="QA fault injection: delay each patient lookup statement by this many ms",
    )
    args = parser.parse_args(argv)
    try:
        profile = _runnable(args.profile)
        secret_dir = bootstrap_django()
        try:
            report = run_profile(
                profile,
                seed=args.seed,
                only=args.only.split(",") if args.only else None,
                inject_slow_query_ms=args.inject_slow_query_ms,
            )
        finally:
            shutil.rmtree(secret_dir, ignore_errors=True)
    except (
        UnknownProfileError,
        ProfileNotRunnableError,
        PerfEnvironmentError,
    ) as error:
        sys.stderr.write(f"perf: {error}\n")
        return 2
    output = (
        args.output
        or ROOT / "var" / "perf" / f"{profile.name}-{report['commit']['sha'][:8]}.json"
    )
    write_report(report, output)
    for item in report["scenarios"]:
        sys.stdout.write(
            f"{item['status']:13} {item['id']:28} p50={item['p50']} "
            f"p95={item['p95']} p99={item['p99']} n={item['samples']} "
            f"err={item['errors']}\n"
        )
    for item in report["fairness"]:
        sys.stdout.write(f"{item['status']:13} {item['id']:28} ratio={item['ratio']}\n")
    sys.stdout.write(f"report: {output}\n")
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
