"""Materialize and validate the child-only Codex review workspace."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    regular_identity,
)
from ops.testing.process_helpers import run_process
from ops.testing.render_review_input import (
    materialize_review_inputs,
    preflight_candidate_tree,
    render_review_prompt,
)
from ops.testing.validate_review_verdict import validate_review_verdict

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
LEDGER = PROJECT_ROOT / ".omo/evidence/isolation-ledger-phase1a.json"
GIT: Final = shutil.which("git")
PRIVATE_DIRECTORY_MODE: Final = 0o700


def main() -> int:
    """Dispatch the closed prepare or finish child form."""
    arguments = _parser().parse_args()
    try:
        if arguments.operation == "prepare":
            _prepare(arguments.lane, arguments.sha, arguments.verdict)
        else:
            _finish(arguments.lane, arguments.sha, arguments.verdict, arguments.raw)
    except (IsolationError, OSError, ValueError) as error:
        sys.stderr.write(f"review-gate: {error}\n")
        return 2
    return 0


def _prepare(lane: str, sha: str, verdict: Path) -> None:
    root = _workspace_root(lane, verdict)
    preflight_candidate_tree(PROJECT_ROOT, sha)
    clone = root / "clone"
    if GIT is None:
        _fail("git executable is unavailable")
    result = run_process(
        (GIT, "clone", "--no-local", "--no-hardlinks", str(PROJECT_ROOT), str(clone)),
        timeout_seconds=120,
    )
    if result.returncode != 0:
        _fail("detached review clone failed")
    result = run_process((GIT, "-C", str(clone), "checkout", "--detach", sha))
    if result.returncode != 0:
        _fail("detached review checkout failed")
    manifest = root / "staging/review-manifest.json"
    approved, sidecar, frozen, receipts = _manifest_sources(manifest)
    materialize_review_inputs(clone, approved, sidecar, frozen, receipts)
    prompt = root / "staging/review-prompt.txt"
    _write_once(prompt, render_review_prompt(lane, sha, clone), 0o400)
    codex = root / "tool-bin/codex"
    codex_home = root / "codex-home"
    regular_identity(codex, mode=0o555)
    if (
        codex_home.is_symlink()
        or codex_home.stat().st_mode & 0o777 != PRIVATE_DIRECTORY_MODE
    ):
        _fail("Codex home identity is invalid")
    for value in (codex, clone, codex_home, root / "staging/raw-verdict.json", prompt):
        sys.stdout.write(f"{value}\n")


def _finish(lane: str, sha: str, verdict: Path, raw: Path | None) -> None:
    root = _workspace_root(lane, verdict)
    expected_raw = root / "staging/raw-verdict.json"
    if raw is None or raw != expected_raw or raw.is_symlink():
        _fail("raw verdict path is invalid")
    validated = validate_review_verdict(raw.read_bytes(), lane, sha)
    _write_once(verdict, validated, MODE_IMMUTABLE)
    receipt: JsonObject = {
        "input_sha256": _sha(root / "staging/review-manifest.json"),
        "lane": lane,
        "schema_version": 1,
        "session_fresh": True,
        "sha": sha,
        "verdict_sha256": _sha(verdict),
    }
    _write_once(
        root / "staging/review-runner.json", canonical_bytes(receipt), MODE_IMMUTABLE
    )


def _workspace_root(lane: str, verdict: Path) -> Path:
    claim_id = os.environ.get("CLINIC_REVIEW_WORKSPACE_CLAIM_ID", "")
    ledger, _ = load_json(LEDGER)
    attempt_root = ledger.get("attempt_root")
    claims = ledger.get("claims")
    if not isinstance(attempt_root, str) or not isinstance(claims, list):
        _fail("review workspace ledger is invalid")
    claim = next(
        (
            item
            for item in claims
            if isinstance(item, dict) and item.get("claim_id") == claim_id
        ),
        None,
    )
    purpose = f"{lane.casefold()}-review-workspace"
    if (
        not isinstance(claim, dict)
        or claim.get("status") != "active"
        or claim.get("purpose") != purpose
    ):
        _fail("review workspace claim is not active")
    desired = claim.get("desired")
    if not isinstance(desired, dict) or desired.get("published_outputs") != []:
        _fail("review child received publication authority")
    root = Path(attempt_root) / "claims" / claim_id
    if root.is_symlink() or root.stat().st_mode & 0o777 != PRIVATE_DIRECTORY_MODE:
        _fail("review workspace root identity is invalid")
    expected = root / f"staging/{lane}-verdict.json"
    if verdict != expected:
        _fail("review verdict is outside the authenticated staging root")
    return root


def _manifest_sources(path: Path) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    regular_identity(path, mode=MODE_IMMUTABLE)
    value, _ = load_json(path)
    if set(value) != {
        "approved_plan",
        "approved_sidecar",
        "frozen_manifest",
        "receipts",
    }:
        _fail("review manifest has an open root")
    receipts = value.get("receipts")
    if not isinstance(receipts, list) or not all(
        isinstance(item, str) for item in receipts
    ):
        _fail("review receipt allowlist is invalid")
    return (
        _absolute(value.get("approved_plan")),
        _absolute(value.get("approved_sidecar")),
        _absolute(value.get("frozen_manifest")),
        tuple(_absolute(item) for item in receipts),
    )


def _absolute(value: JsonValue) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail("review input source path is invalid")
    return Path(value)


def _write_once(path: Path, raw: bytes, mode: int) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("prepare", "finish"))
    parser.add_argument("--lane", required=True, choices=("F1", "F2"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--verdict", required=True, type=Path)
    parser.add_argument("--raw", type=Path)
    return parser


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
