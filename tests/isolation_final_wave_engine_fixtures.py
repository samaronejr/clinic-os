from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Final

from ops.testing import isolation_common as c
from ops.testing import isolation_controller_kernel as kernel
from ops.testing import isolation_final_wave_controller as final_controller
from ops.testing import isolation_final_wave_record as final_record
from ops.testing import isolation_review_lane_controller as review
from ops.testing import isolation_review_lane_record as review_record

ATTEMPT: Final = "11111111-1111-4111-8111-111111111111"
BOOT: Final = "22222222-2222-4222-8222-222222222222"
WORKSPACE_CLAIM: Final = "44444444-4444-4444-8444-444444444444"
PRIVATE_CLAIM: Final = "55555555-5555-4555-8555-555555555555"
PROCESS_CLAIMS: Final = (
    "55555555-5555-4555-8555-555555555551",
    "55555555-5555-4555-8555-555555555552",
    "55555555-5555-4555-8555-555555555553",
)
SHA: Final = "a" * 40
DIGEST: Final = "c" * 64
NOW: Final = "2026-07-16T12:00:00.000000Z"
LATER: Final = "2026-07-16T12:01:00.000000Z"
ALIVE: Final = kernel.RecoveryPolicy(lambda _pid, _ticks: True)
FALSE: Final = False
PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]


def review_start(
    root: Path, lane: review_record.ReviewLane, pid: int
) -> review_record.ReviewLaneStart:
    controller_root = root / "review-lane-controllers"
    controller_root.mkdir(parents=True, exist_ok=True)
    private = None if lane == "F1" else PRIVATE_CLAIM
    return review_record.ReviewLaneStart(
        ATTEMPT,
        lane,
        SHA,
        DIGEST,
        BOOT,
        controller_root / f"{lane}.json",
        controller_root / f"{lane}.lease",
        owner_pid=pid,
        owner_start_ticks=pid + 4000,
        acquired_at_utc=NOW,
        review_workspace_claim_id=WORKSPACE_CLAIM,
        private_environment_claim_id=private,
    )


def completed_review(
    root: Path,
    lane: review_record.ReviewLane,
    exit_codes: tuple[int, ...],
    pid: int,
) -> review.ReviewLaneSession:
    root.mkdir(parents=True, exist_ok=True)
    session = review.acquire_review_lane(review_start(root, lane, pid), ALIVE)
    session.reserve_filesystems(NOW)
    session.activate_filesystems(
        review.ToolClosure(DIGEST, DIGEST if lane == "F2" else None), NOW
    )
    for index, exit_code in enumerate(exit_codes):
        identity = kernel.ChildIdentity(
            pid + index + 1, pid + index + 1, pid + index + 8000
        )
        session.reserve_child_claim(PROCESS_CLAIMS[index], DIGEST, NOW)
        session.record_child(
            review.ReviewChild(PROCESS_CLAIMS[index], DIGEST, identity), NOW
        )
        session.release_child(NOW)
        session.record_wait(kernel.WaitResult(exit_code, None, FALSE, DIGEST), NOW)
        tree = DIGEST if lane == "F2" and index == 0 else None
        session.advance_stage(NOW, tree)
    return session


def sealed_review(
    root: Path,
    lane: review_record.ReviewLane,
    exit_codes: tuple[int, ...],
    pid: int,
    *,
    success: bool,
) -> c.JsonObject:
    session = completed_review(root, lane, exit_codes, pid)
    terminal = DIGEST if success else None
    session.seal(review.ReviewSeal(success, LATER, terminal, terminal))
    return session.record.copy()


def successful_predecessor_evidence(root: Path, pid: int) -> final_record.PreF4Evidence:
    f1 = sealed_review(root / "f1", "F1", (0,), pid, success=True)
    f2 = sealed_review(root / "f2", "F2", (0, 0, 0), pid + 10, success=True)
    return final_record.PreF4Evidence(DIGEST, f1, f2, DIGEST, DIGEST)


def final_start(
    root: Path,
    boot: str,
    pid: int,
    form: final_record.FinalForm = "inputs",
) -> final_record.FinalWaveStart:
    root.mkdir(parents=True, exist_ok=True)
    downstream = form in {"pre-f4", "final"}
    return final_record.FinalWaveStart(
        ATTEMPT,
        SHA,
        form,
        boot,
        root / "final-wave-controller.json",
        root / "final-wave-controller.lease",
        DIGEST,
        pid,
        pid + 4000,
        NOW,
        final_gate_staging_claim_id=WORKSPACE_CLAIM if form == "final" else None,
        predecessor_evidence=(
            successful_predecessor_evidence(root, pid + 100) if downstream else None
        ),
        pre_f4_sha256=DIGEST if form == "final" else None,
    )


def final_record_matrix(root: Path) -> dict[str, c.JsonObject]:
    session = final_controller.acquire_final_wave(
        final_start(root / "success", BOOT, 5000, "final"), ALIVE
    )
    records = {"prepared": copy.deepcopy(session.record)}
    session.reserve_final_staging(NOW)
    session.activate_final_staging(NOW)
    session.record_child(kernel.ChildIdentity(5001, 5001, 9001), NOW)
    session.release_child(NOW)
    session.record_wait(kernel.WaitResult(0, None, FALSE, DIGEST), NOW)
    session.advance_final_decision((DIGEST, DIGEST, DIGEST), LATER)
    records["between-stages"] = copy.deepcopy(session.record)
    session.record_child(kernel.ChildIdentity(5002, 5002, 9002), LATER)
    session.release_child(LATER)
    session.record_wait(kernel.WaitResult(0, None, FALSE, DIGEST), LATER)
    records["child-terminal"] = copy.deepcopy(session.record)
    session.seal_success(LATER)
    records["success"] = copy.deepcopy(session.record)

    failure = final_controller.acquire_final_wave(
        final_start(root / "failure", BOOT, 5100, "final"), ALIVE
    )
    failure.reserve_final_staging(NOW)
    failure.activate_final_staging(NOW)
    failure.record_child(kernel.ChildIdentity(5101, 5101, 9101), NOW)
    failure.release_child(NOW)
    failure.record_wait(kernel.WaitResult(1, None, FALSE, DIGEST), NOW)
    failure.seal_failure(LATER)
    records["failure-ready"] = copy.deepcopy(failure.record)
    return records


def schema_result(
    root: Path, index: int, record: c.JsonObject
) -> subprocess.CompletedProcess[str]:
    instance = root / f"final-wave-{index}.json"
    _ = instance.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    validator = shutil.which("jsonschema")
    assert validator is not None
    return subprocess.run(  # noqa: S603 - fixed validator and test-owned input.
        (
            validator,
            "-V",
            "Draft202012Validator",
            str(PROJECT_ROOT / "ops/testing/final-wave-controller-state.schema.json"),
            "-i",
            str(instance),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
