"""Build and publish the final-input manifest from fixed ledger authorities."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Never

from ops.testing.cgroup_capability_probe import ProbeRequest, run_capability_probe
from ops.testing.f3_launcher_manifest import input_sidecar_bytes, launcher_prefix_bytes
from ops.testing.isolation_claim_records import claim_objects
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_final_input_freezer import (
    CandidateInspection,
    FinalInputFreeze,
    SourceInspection,
    freeze_final_inputs,
)
from ops.testing.isolation_terminal_publication import publish_terminal_file
from ops.testing.process_helpers import run_process

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = PROJECT_ROOT / ".omo/evidence/isolation-ledger-phase1a.json"


def build_final_input_request(
    sha: str, control_root: Path, output: Path
) -> FinalInputFreeze:
    """Derive the complete freezer request from the open authoritative ledger."""
    ledger, _ = load_json(LEDGER_PATH)
    attempt_id = _text(ledger.get("attempt_id"), "attempt ID")
    attempt_root = _absolute(ledger.get("attempt_root"), "attempt root")
    worktree = _absolute(ledger.get("worktree_realpath"), "worktree")
    expected_control = attempt_root.parents[1] / "clinic-os-phase1a-final"
    if (
        control_root != expected_control
        or output != control_root / "terminal/inputs.json"
    ):
        _fail("final input controller paths differ from fixed authorities")
    approved = _object(ledger.get("approved_plan"), "approved plan")
    proof = _object(ledger.get("execution_host_preflight"), "execution proof")
    baseline = _object(ledger.get("baseline"), "baseline")
    shared = _object(baseline.get("shared_evidence_manifest"), "shared baseline")
    proof_path = _absolute(proof.get("path"), "execution proof path")
    proof_raw = proof_path.read_bytes()
    final_probe = run_capability_probe(
        ProbeRequest(
            attempt_id,
            attempt_root,
            proof_path,
            raw_sha256(proof_raw),
            "final-input-freeze",
        )
    )
    candidate_root = attempt_root / "candidate-images" / sha
    application_path = candidate_root / "application-envelope.json"
    runner_path = candidate_root / "browser-runner-envelope.json"
    application, _ = load_json(application_path)
    runner, _ = load_json(runner_path)
    application_claim = _text(application.get("claim_id"), "application claim ID")
    runner_claim = _text(runner.get("claim_id"), "runner claim ID")
    publishers = [
        item
        for item in claim_objects(ledger["claims"])
        if item.get("purpose") == "final-terminal-publisher"
        and item.get("status") == "active"
    ]
    if len(publishers) != 1:
        _fail("final inputs require one active terminal publisher")
    publisher_id = _text(publishers[0].get("claim_id"), "publisher claim ID")
    codex = _executable("codex")
    uv = _executable("uv")
    source = _source_inspection(worktree, sha)
    return FinalInputFreeze(
        worktree,
        sha,
        source.tree_sha,
        attempt_id,
        attempt_root,
        _absolute(approved.get("path"), "approved plan path"),
        proof_path,
        LEDGER_PATH,
        _absolute(shared.get("path"), "shared baseline path"),
        attempt_root / "execution-host-probes/todo1-kickoff.json",
        final_probe,
        attempt_root / "todo-evidence",
        application_path,
        attempt_root / f"publication-history/{application_claim}.json",
        runner_path,
        attempt_root / f"publication-history/{runner_claim}.json",
        CandidateInspection(
            _text(application.get("image_id"), "application image ID"),
            _object(application.get("image_contract"), "application image contract"),
        ),
        CandidateInspection(
            _text(runner.get("image_id"), "runner image ID"),
            _object(runner.get("image_contract"), "runner image contract"),
        ),
        attempt_root / "claims" / publisher_id,
        output,
        source,
        codex,
        uv,
        worktree / "ops/testing/final-required-browser-suites.txt",
    )


def main() -> int:
    """Dispatch the exact outer-controller input-freezer child form."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--control-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        request = build_final_input_request(
            arguments.sha, arguments.control_root.resolve(), arguments.output.resolve()
        )
        _publish_launcher_prefix(request)
        _ = freeze_final_inputs(request)
        _publish_input_sidecar(request)
    except (IsolationError, OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"freeze-final-inputs: {error}\n")
        return 2
    return 0


def _source_inspection(worktree: Path, sha: str) -> SourceInspection:
    git = _executable("git")
    head = run_process((str(git), "-C", str(worktree), "rev-parse", "HEAD"))
    tree = run_process((str(git), "-C", str(worktree), "rev-parse", f"{sha}^{{tree}}"))
    status = run_process(
        (
            str(git),
            "-C",
            str(worktree),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    )
    if head.returncode or tree.returncode or status.returncode:
        _fail("final input source inspection command failed")
    observed_head, observed_tree = head.stdout.strip(), tree.stdout.strip()
    return SourceInspection(observed_head, observed_tree, not status.stdout)


def _publish_launcher_prefix(request: FinalInputFreeze) -> None:
    launcher = request.worktree / ".venv/bin/python"
    path_raw, lstat_raw, sha256_raw = launcher_prefix_bytes(
        launcher,
        request.worktree / "ops/testing/final_e2e.sh",
        request.worktree / "ops/testing/final_e2e_supervisor.py",
        request.worktree / "ops/testing/final_e2e_controller.py",
    )
    publications = (
        ("input-launcher-path", path_raw),
        ("input-launcher-lstat", lstat_raw),
        ("input-launcher-sha256", sha256_raw),
    )
    _publish_staged(request, publications)


def _publish_input_sidecar(request: FinalInputFreeze) -> None:
    terminal = request.output_path.parent
    raw = input_sidecar_bytes(
        request.output_path,
        terminal / "F3-launcher.path",
        terminal / "F3-launcher.lstat",
        terminal / "F3-launcher.sha256",
    )
    _publish_staged(request, (("input-sidecar", raw),))


def _publish_staged(
    request: FinalInputFreeze, publications: tuple[tuple[str, bytes], ...]
) -> None:
    for authorization, raw in publications:
        staged = request.staging_root / f".{authorization}.staged"
        if staged.exists():
            if staged.read_bytes() != raw:
                _fail("final input staged publication replay drifted")
        else:
            write_no_replace(staged, raw, mode=MODE_IMMUTABLE)
        _ = publish_terminal_file(
            request.ledger_path,
            request.staging_root.name,
            authorization,
            staged,
            request.output_path.parent,
        )
        staged.unlink()


def _executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        _fail(f"required {name} launcher is unavailable")
    return Path(value)


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} is not an object")
    return value


def _absolute(value: JsonValue, context: str) -> Path:
    text = _text(value, context)
    path = Path(text)
    if not path.is_absolute():
        _fail(f"{context} is not absolute")
    return path


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} is not text")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
