"""Perf report assembly: pass/fail against PF targets and schema validation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from apps.core.json_schema import schema_validator

from ops.perf.profiles import (
    FAIRNESS_TOLERANCE,
    NOT_MEASURED,
    Profile,
    target_for,
)
from ops.perf.stats import summarize

if TYPE_CHECKING:
    from collections.abc import Sequence

SCHEMA_PATH = Path(__file__).with_name("report-schema.json")
CLAIM_SCOPE = (
    "L1 local synthetic evidence on the named profile; load is generated on one "
    "workstation against a throwaway database. It is not a production SLO and "
    "does not show any PF target met in production."
)
Kind = Literal["measured", "component", "modeled"]


class ReportError(ValueError):
    """Reject a report that violates the schema."""


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Raw outcome of one scenario before it becomes a report record."""

    scenario_id: str
    kind: Kind
    samples_ms: list[float]
    errors: int = 0
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def scenario_record(result: ScenarioResult) -> dict[str, Any]:
    """Turn raw samples into a record with its pass/fail against the PF target."""
    target = target_for(result.scenario_id)
    record: dict[str, Any] = {
        "id": result.scenario_id,
        "kind": result.kind,
        "unit": "ms",
        "samples": len(result.samples_ms),
        "errors": result.errors,
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
        "target": None
        if target is None
        else {
            "metric": "p95",
            "limit_ms": target.limit_ms,
            "source": target.source,
        },
        "notes": result.notes,
    }
    if not result.samples_ms:
        record["status"] = "fail"
        return record
    summary = summarize(result.samples_ms)
    record.update(
        p50=round(summary.p50, 3),
        p95=round(summary.p95, 3),
        p99=round(summary.p99, 3),
        max=round(summary.maximum, 3),
    )
    if target is None or result.kind == "modeled":
        # A model is arithmetic over assumed timings: never a target met.
        record["status"] = "informational"
    elif result.errors or summary.p95 > target.limit_ms:
        record["status"] = "fail"
    else:
        record["status"] = "pass"
    return record


def fairness_record(  # noqa: PLR0913 - one verdict's named measurements
    *,
    fairness_id: str,
    baseline_p95_ms: float,
    loaded_p95_ms: float,
    trials: int,
    load: dict[str, Any],
    also_required: bool = True,
    notes: str = "",
) -> dict[str, Any]:
    """Judge a protected workload: loaded p95 within tolerance of baseline.

    ``also_required`` carries the scenario's other hard conditions (for example
    the victim tenant being admitted on the flooded queue).
    """
    ratio = loaded_p95_ms / baseline_p95_ms if baseline_p95_ms > 0 else float("inf")
    return {
        "id": fairness_id,
        "baseline_p95_ms": round(baseline_p95_ms, 3),
        "loaded_p95_ms": round(loaded_p95_ms, 3),
        "ratio": round(ratio, 4),
        "tolerance": FAIRNESS_TOLERANCE,
        "status": "pass"
        if also_required and ratio <= 1 + FAIRNESS_TOLERANCE
        else "fail",
        "trials": trials,
        "load": load,
        "notes": notes,
    }


def commit_identity(root: Path) -> dict[str, Any]:
    """Return the HEAD sha and whether tracked files differ from it."""
    sha = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return {"sha": sha, "dirty": dirty}


def build_report(  # noqa: PLR0913 - the report's named sections
    *,
    profile: Profile,
    dataset: dict[str, Any],
    commit: dict[str, Any],
    scenarios: Sequence[dict[str, Any]],
    fairness: Sequence[dict[str, Any]],
    search_benchmark: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one schema-valid report; raise when it is not valid."""
    statuses = [item["status"] for item in scenarios]
    statuses += [item["status"] for item in fairness]
    report: dict[str, Any] = {
        "schema_version": "clinic-perf-report/1",
        "profile": profile.name,
        "dataset": dataset,
        "commit": commit,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "evidence_level": "L1",
        "claim_scope": CLAIM_SCOPE,
        "environment": environment or {},
        "scenarios": list(scenarios),
        "fairness": list(fairness),
        "not_measured": [
            {"target": target, "reason": reason} for target, reason in NOT_MEASURED
        ],
        "summary": {
            "passed": statuses.count("pass"),
            "failed": statuses.count("fail"),
            "informational": statuses.count("informational"),
        },
    }
    if search_benchmark is not None:
        report["search_benchmark"] = search_benchmark
    validate_report(report)
    return report


def validate_report(report: dict[str, Any]) -> None:
    """Validate against ops/perf/report-schema.json."""
    if not schema_validator(SCHEMA_PATH).is_valid(report):
        message = "perf report violates ops/perf/report-schema.json"
        raise ReportError(message)


def write_report(report: dict[str, Any], path: Path) -> None:
    """Write the validated report as sorted, indented JSON."""
    validate_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", "utf-8")
