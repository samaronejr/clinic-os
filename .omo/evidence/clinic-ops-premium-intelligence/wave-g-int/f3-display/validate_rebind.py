"""Read-only rebind refusals, staged final plans and launcher-lesson regressions."""

import ast
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import auxiliary
import completion_state
import driver
import f2_entry
import rebind_head
from artifacts import require
from head_binding import (
    BASELINE, F5_REPAIR, F5_PREDECESSOR, REQUIRED_ANCESTORS,
    ORDER, REJECTED, build_binding, selection_bytes, validate_binding,
)
from launch_guards import CLEAN_ENVIRONMENT_KEYS, require_clean_environment
from validate import decode_many, execution_state, refused
from validate_f2 import validate_f2
from validate_fairness import validate_fairness
from f5_eval_entry import preparation as prepare_f5

# Positive regression input only. The actual historical keeper and the
# launcher's required ancestry, previous-head refusals and manifests are unchanged.
CANDIDATE = "bcf9417d8de575ea93be4be939732b63cd90ac3e"


def main():
    original = driver.BASE.read_bytes()
    old = json.loads(original)
    state_before = execution_state()
    selection_before = (driver.E / "f2-selection.json").read_bytes()
    for path in driver.E.glob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
    refused_run = subprocess.run([
        sys.executable, "-B", str(driver.E / "rebind_head.py"), "--rebind-head", BASELINE,
    ], check=False, capture_output=True, text=True)
    require(refused_run.returncode == 1 and "REBIND_REFUSED:" in refused_run.stderr,
            "The old 7045155a stand-in was rebound")
    ancestry = subprocess.run([
        "git", "-C", str(driver.shared.runtime.INTEGRATION),
        "merge-base", "--is-ancestor", CANDIDATE, BASELINE,
    ], check=False, capture_output=True)
    require(ancestry.returncode == 1, "The stand-in no longer demonstrates missing ancestry")
    with patch.object(driver.shared.runtime, "git", return_value=BASELINE):
        refused(lambda: rebind_head.check_head(CANDIDATE))
    with patch.object(driver.shared.runtime, "git", return_value=CANDIDATE):
        rebind_head.check_head(CANDIDATE)

    # The staged positive fixture is an existing descendant; no ref or manifest is changed.
    refused(lambda: prepare_f5(F5_REPAIR))
    collected = json.loads((driver.E / old["broker_collection"]["record"]).read_bytes())
    collected["head"] = CANDIDATE
    collected_bytes = json.dumps(collected).encode()
    collected_path = driver.E / ".f5-broker-fixture.json"
    broker_binding = {"record": collected_path.name,
                      "record_sha256": hashlib.sha256(collected_bytes).hexdigest(), "expected": 38}
    staged = build_binding(old, CANDIDATE, driver.E, hashlib.sha256(original).hexdigest(),
                           broker_binding=broker_binding)
    require(staged["rejected_heads"] == [REJECTED, F5_PREDECESSOR]
            and any(row["head"] == BASELINE for row in staged["history"])
            and staged["booking_order"] == ORDER
            and staged["native_r_prerequisite"]["required_before"] == ORDER[1:],
            "The history, sole F2 rejection or native-first reset is wrong")
    for key, bad in (
        ("rejected_heads", [REJECTED, BASELINE]),
        ("final_evidence_root", old.get("final_evidence_root", "final-wave")),
        ("screenshot_root", old["screenshot_root"]),
    ):
        invalid = copy.deepcopy(staged)
        invalid[key] = bad
        refused(lambda: validate_binding(invalid, driver.E))
    for field, bad in (("perf_realtime_enabled", False),
                       ("completion_watch", "is-active"),
                       ("clean_environment_keys", [*sorted(CLEAN_ENVIRONMENT_KEYS), "HEAD"])):
        invalid = copy.deepcopy(staged)
        invalid["rebind"][field] = bad
        refused(lambda: validate_binding(invalid, driver.E))

    real_read = Path.read_bytes
    staged_bytes = (json.dumps(staged, indent=2) + "\n").encode()

    def staged_read(path):
        if path == collected_path:
            return collected_bytes
        return staged_bytes if path == driver.BASE else real_read(path)

    import head_binding
    original_regular = head_binding.regular
    with patch.object(head_binding, "regular", side_effect=lambda path:
                      path if path == collected_path else original_regular(path)), \
            patch.object(Path, "read_bytes", autospec=True, side_effect=staged_read), patch.object(
        driver, "target", return_value=CANDIDATE
    ), patch.object(driver, "probe_environment", return_value={
        "status": "passed", "keys": sorted(CLEAN_ENVIRONMENT_KEYS),
    }):
        settings = driver.configuration()
        require(f2_entry.selection()["head"] == CANDIDATE,
                "The final F2 selection was not rebound atomically")
        previews = []
        for block in ORDER:
            request = driver.request_for(block, "now", now=datetime.now(UTC))
            require(not request["credited_cells"]
                    and request["native_r_predecessor_sha256"] is None,
                    "Old native/F3 credit leaked into the new head")
            if block != "native-r":
                require(any("Native R" in row for row in request["launch_blockers"]),
                        "A final block bypassed the new native R")
            specification = driver.plan(
                driver.run_path(block, CANDIDATE), block, CANDIDATE, request,
            )
            require(specification["head"] == CANDIDATE and not specification["head_blockers"]
                    and CANDIDATE[:8] in specification["runtime"]
                    and all(not Path(specification[key]).exists() for key in (
                        "output", "runtime", "worktree", "manifest", "aggregate_root",
                    )), "A staged final block reused prior execution state")
            if block in {"f4-fairness", "f2-retest-a", "f2-retest-b",
                         "f6-a", "f6-b", "f6-c", "f6-d", "f7-side-effects"}:
                require(str(auxiliary.output_for(block)).startswith(str(
                    driver.ROOT / staged["final_evidence_root"]
                )), "A final output would overwrite historical evidence")
            previews.append(specification)
    with patch.object(f2_entry, "selection_bytes", return_value=selection_bytes(staged, driver.E)), \
            patch.object(f2_entry, "output_for", side_effect=lambda block: (
                driver.ROOT / staged["f2"]["output_root"] / block
            )):
        f2_checks = validate_f2(settings, staged["f2"]["selection"], refused)
    perf_checks = validate_fairness(settings, previews, refused, broker_selection=collected)

    # A shared strict guard rejects the exact extra HEAD that broke candidate execute.
    clean = {key: "fixture-only" for key in CLEAN_ENVIRONMENT_KEYS}
    with patch.dict(os.environ, clean, clear=True):
        require_clean_environment()
        os.environ["HEAD"] = BASELINE
        refused(require_clean_environment)
    origin = driver.shared.runtime.git("rev-parse", "refs/remotes/origin/ulw/cpi-wave-h")
    if origin == BASELINE:
        normal = decode_many(subprocess.check_output([
            "bash", str(driver.E / "chain.sh"), "--block", "native-r", "--dry-plan",
        ], text=True, env={**os.environ, "HEAD": BASELINE, "START": "now"}))[0]
        require(normal["execute_environment_preflight"]["status"] == "passed",
                "The final entrypoint did not check its exact clean launch environment")

    result = {
        "specification": {"head": CANDIDATE, "id": "fixture-native"},
        "status": "complete", "exit_code": 0, "cleanup": {"status": "complete"},
    }
    unit = {
        "ActiveState": "active", "SubState": "exited",
        "LoadState": "loaded", "Result": "success", "ExecMainStatus": "0",
    }
    require(completion_state.classify(unit, result, CANDIDATE, "fixture-native")["passed"],
            "A successful RemainAfterExit unit was mistaken for a running workload")
    require(not completion_state.classify(
        {**unit, "SubState": "running"}, result, CANDIDATE, "fixture-native",
    )["finished"], "A running unit was marked finished")
    for bad_result in (
        None, {**result, "exit_code": 1},
        {**result, "cleanup": {"status": "incomplete"}},
        {**result, "diagnostic": True}, {**result, "credit_eligible": False},
    ):
        require(not completion_state.classify(
            unit, bad_result, CANDIDATE, "fixture-native",
        )["passed"], "An incomplete or diagnostic RESULT satisfied completion")
    require(not completion_state.classify(
        {**unit, "SubState": "failed", "Result": "exit-code"}, result, CANDIDATE, "fixture-native",
    )["passed"], "A failed systemd unit was accepted")
    refused(lambda: completion_state.classify(unit, result, BASELINE, "fixture-native"))

    # Exercise publication and rollback entirely in memory while the real host is busy.
    lock = driver.ROOT / ".omo/locks/heavy.lock"
    real_open = Path.open
    # A paired installation must never enter the simulated *single-manifest*
    # rollback path below. Check that refusal separately before modeling the
    # unpaired input for which the historical four rollback cases are defined.
    with patch.object(driver, "BINDING_GENERATION", "fixture-installed-pair"):
        refused(lambda: rebind_head.rebind(CANDIDATE))
    for case in ("pass", "validation-failed", "wrong-head-report", "malformed-report"):
        storage = [original]
        writes = []

        def replace(expected, replacement):
            require(storage[0] == expected, "Concurrent metadata was overwritten")
            storage[0] = replacement
            writes.append(replacement)

        def read_storage(path):
            return collected_bytes if path == collected_path else (
                storage[0] if path == driver.BASE else real_read(path)
            )

        def open_lock(path, *args, **kwargs):
            return io.StringIO() if path == lock else real_open(path, *args, **kwargs)

        report = {
            "status": "preparation_checks_passed", "validated_head": CANDIDATE,
            "blocks": len(ORDER), "databases_started": 0, "browsers_started": 0, "units_started": 0,
        }
        if case == "wrong-head-report":
            report["validated_head"] = BASELINE
        text = json.dumps(report) if case != "malformed-report" else "invalid JSON"
        with patch.object(driver, "BINDING_GENERATION", None), \
                patch.object(rebind_head, "check_head"), \
                patch.object(rebind_head, "collect_broker", return_value=broker_binding), patch.object(
            rebind_head, "replace_manifest", side_effect=replace
        ), patch.object(Path, "read_bytes", autospec=True, side_effect=read_storage), patch.object(
            Path, "open", autospec=True, side_effect=open_lock
        ), patch.object(rebind_head.fcntl, "flock"), patch.object(
            rebind_head, "subprocess", wraps=subprocess
        ) as validator_process:
            # Only the rebind validator subprocess is simulated. The independent
            # Git reader must still obtain actual binary blobs from this tree.
            validator_process.run.return_value = subprocess.CompletedProcess(
                [], 1 if case == "validation-failed" else 0, text, "",
            )
            if case == "pass":
                require(rebind_head.rebind(CANDIDATE)["units_launched"] == 0
                        and json.loads(storage[0])["reviewed_head"] == CANDIDATE,
                        "The single validated rebind did not publish its reviewed head")
            else:
                refused(lambda: rebind_head.rebind(CANDIDATE))
                require(storage[0] == original and len(writes) == 2,
                        "Failed validation did not restore the previous manifest")

    require(driver.BASE.read_bytes() == original and execution_state() == state_before
            and (driver.E / "f2-selection.json").read_bytes() == selection_before,
            "Preparation validation changed the current binding or historical evidence")
    from validate_f5_eval import validate_f5_eval
    eval_checks = validate_f5_eval(staged["f5_eval"], refused)
    return {
        "status": "rebind_preparation_checks_passed",
        "F5_E1_checks": eval_checks,
        "stand_in": BASELINE, "stand_in_refused": True,
        "stand_in_reason": refused_run.stderr.strip(),
        "required_ancestors": REQUIRED_ANCESTORS,
        "staged_final_blocks": len(ORDER),
        "history_and_credit_reset": "passed",
        "atomic_publish_and_failed_validation_rollback": "passed",
        "RemainAfterExit_SubState_RESULT_completion": "passed",
        "shared_exact_clean_environment": "passed",
        "F2_rebound_selection_checks": f2_checks,
        "perf_realtime_and_full_workload_checks": perf_checks,
        "current_manifest_unchanged": True,
        "order_and_estimates": [
            {"block": block, "minutes": next(
                row["estimated_minutes"] for row in staged["units"] if row["block"] == block
            )} for block in ORDER
        ],
        "databases_started": 0, "browsers_started": 0, "units_started": 0,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
