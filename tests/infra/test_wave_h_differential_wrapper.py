"""Integration contract for the opt-in, claimed-PostgreSQL Wave H wrapper."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = "61918080021ddcfe2912e6922677143d5704d96d"
FOUNDATION = "b39ae436590c8a9c00ad8fc93d619875d1822827"


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WAVE_H_DIFFERENTIAL_E2E") != "1",
    reason="requires a claimed PostgreSQL instance and locked base environments",
)


def _run_wrapper(base: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UPGRADE_WAVE_H_")
    }
    if base != BASE:
        env.pop("UPGRADE_REUSE_ENVIRONMENT", None)
    return subprocess.run(  # noqa: S603 - fixed repository harness and pinned base
        [str(ROOT / "ops/testing/migration_upgrade.sh"), base],
        cwd=ROOT,
        env={
            **env,
            "GITHUB_WORKSPACE": str(ROOT),
            "RUNNER_TEMP": str(tmp_path),
            "UPGRADE_WAVE_H_DIFFERENTIAL": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


def test_wrapper_generates_native_probe_cases_and_publishes_verdict(
    tmp_path: Path,
) -> None:
    result = _run_wrapper(BASE, tmp_path)
    receipt = tmp_path / "migration-upgrade"
    verdict_path = receipt / "verdict.json"
    assert verdict_path.is_file(), result.stdout[-6000:]
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert result.returncode == 0, (verdict, result.stdout[-6000:])
    assert verdict["result"] == "PASS"
    assert verdict["base_sha"] == BASE
    assert verdict["database_recreated"] is False
    assert verdict["differential"] == {
        "result": "PASS",
        "projection_cases": 3844,
        "probe_window_cases": 118,
        "differences": [],
        "base_reject_lane_pass_pairs": [],
    }
    assert not (receipt / "base").exists()
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))


def test_wrapper_names_probe_case_generation_failure(tmp_path: Path) -> None:
    result = _run_wrapper(FOUNDATION, tmp_path)
    receipt = tmp_path / "migration-upgrade"
    verdict_path = receipt / "verdict.json"
    assert verdict_path.is_file(), result.stdout[-6000:]
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert verdict["result"] == "FAIL"
    assert verdict["reason"] == "candidate_probe_case_generation"
    assert verdict["differential"]["probe_window_cases"] == 0
    assert not (receipt / "base").exists()
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))


def test_wrapper_preserves_stale_pending_directory_and_publishes_failure(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "migration-upgrade"
    stale = receipt / "seed.pending"
    stale.mkdir(parents=True)
    unowned = stale / "keep"
    unowned.write_text("unowned", encoding="utf-8")
    verdict_path = receipt / "verdict.json"
    verdict_path.write_text('{"result":"PASS"}\n', encoding="utf-8")

    result = _run_wrapper(BASE, tmp_path)

    assert result.returncode == 1
    assert unowned.read_text(encoding="utf-8") == "unowned"
    assert verdict_path.is_file(), result.stdout[-6000:]
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["result"] == "FAIL"
    assert verdict["reason"] == "stale_seed_pending"
    assert not (receipt / "base").exists()
    assert not list(tmp_path.glob("migration-upgrade-secrets.*"))
