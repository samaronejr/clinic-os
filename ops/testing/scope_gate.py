"""Validate the bounded Phase 1A source and infrastructure scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Final, Never

from ops.testing.assert_foundation_history import assert_foundation_history
from ops.testing.inventory_routes import inventory_routes
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonValue,
    load_json,
    regular_identity,
)
from ops.testing.process_helpers import run_process

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
FOUNDATION: Final = "cffbb1900ae2132560f20c27fcf1a514a1ef71aa"
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN_SUFFIXES: Final = (
    ".tfstate",
    ".tfplan",
    ".plan",
    ".tfvars",
    ".tfbackend",
)
GIT: Final = shutil.which("git")


def validate_scope(sha: str, inputs: Path) -> tuple[str, ...]:
    """Check clean identity, frozen inputs, Terraform equality, and routes."""
    if SHA40.fullmatch(sha) is None:
        _fail("scope SHA is invalid")
    regular_identity(inputs, mode=MODE_IMMUTABLE)
    frozen, _ = load_json(inputs)
    tree = _git("rev-parse", f"{sha}^{{tree}}")
    if frozen.get("sha") != sha or frozen.get("tree_sha") != tree:
        _fail("scope frozen input identity differs")
    if _git("rev-parse", "HEAD") != sha or _git(
        "status", "--porcelain=v1", "--untracked-files=all"
    ):
        _fail("scope gate requires a clean matching HEAD")
    assert_foundation_history(PROJECT_ROOT, FOUNDATION)
    _terraform_unchanged(sha)
    _forbidden_residue()
    _allowlist()
    return inventory_routes()


def write_approval(
    evidence: Path, sha: str, inputs: Path, routes: tuple[str, ...]
) -> None:
    """Create one redacted source-precheck approval beneath caller staging."""
    route_hash = hashlib.sha256("\n".join(routes).encode()).hexdigest()
    input_hash = hashlib.sha256(inputs.read_bytes()).hexdigest()
    raw = (
        f"schema_version=1\nsha={sha}\ninputs_sha256={input_hash}\n"
        f"routes_sha256={route_hash}\nAPPROVE-PRECHECK\n"
    ).encode()
    descriptor = os.open(
        evidence,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        MODE_IMMUTABLE,
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _terraform_unchanged(sha: str) -> None:
    if GIT is None:
        _fail("git executable is unavailable")
    result = run_process(
        (
            GIT,
            "-C",
            str(PROJECT_ROOT),
            "diff",
            "--quiet",
            FOUNDATION,
            sha,
            "--",
            "terraform",
        )
    )
    if result.returncode != 0:
        _fail("tracked foundation Terraform changed")


def _forbidden_residue() -> None:
    excluded = {".git", ".omo", ".venv", "__pycache__"}
    for path in PROJECT_ROOT.rglob("*"):
        relative = path.relative_to(PROJECT_ROOT)
        if any(part in excluded for part in relative.parts):
            continue
        name = path.name
        if name == ".terraform" or name.endswith(FORBIDDEN_SUFFIXES):
            _fail("Terraform operational residue is present")


def _allowlist() -> None:
    path = PROJECT_ROOT / "ops/testing/scope_false_positive_allowlist.json"
    value: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    if value != {"entries": [], "schema_version": 1}:
        _fail("scope false-positive allowlist is not the closed empty set")


def _git(*arguments: str) -> str:
    if GIT is None:
        _fail("git executable is unavailable")
    result = run_process((GIT, "-C", str(PROJECT_ROOT), *arguments))
    if result.returncode != 0:
        _fail("scope Git inspection failed")
    return result.stdout.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    return parser


def main() -> int:
    """Validate source scope and stage literal precheck approval."""
    arguments = _parser().parse_args()
    try:
        routes = validate_scope(arguments.sha, arguments.inputs)
        write_approval(arguments.evidence, arguments.sha, arguments.inputs, routes)
    except (IsolationError, OSError, ValueError) as error:
        sys.stderr.write(f"scope-gate: {error}\n")
        return 2
    return 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
