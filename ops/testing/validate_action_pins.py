"""Validate hosted workflow action and repository-snapshot provenance."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Final, Never

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.testing.isolation_common import IsolationError, JsonValue

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SNAPSHOT: Final = (
    "uv run --locked python ops/testing/isolation_ledger.py snapshot --approved-plan "
    '"$GITHUB_WORKSPACE/docs/plans/clinic-os-phase1a-approved.md" '
    "--tracked-ci-sidecar "
    '"$GITHUB_WORKSPACE/docs/plans/clinic-os-phase1a-approved.sha256" '
    "--foundation-sha cffbb1900ae2132560f20c27fcf1a514a1ef71aa "
    '--worktree "$GITHUB_WORKSPACE"'
)
ACTION_MAP: Final = {
    "actions/checkout": (
        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        "v7.0.0",
    ),
    "astral-sh/setup-uv": (
        "11f9893b081a58869d3b5fccaea48c9e9e46f990",
        "v8.3.2",
    ),
    "raven-actions/actionlint": (
        "3d39aea434753780c3b3d4a1a31c854b4dbf49d7",
        "v2.2.0",
    ),
}
USE = re.compile(
    r"^\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})\s+#\s+(v\S+)\s*$"
)
JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
STEP = re.compile(r"^      - (?:name|uses|run):")
DATABASE_CONSUMERS: Final = (
    "make db-bootstrap",
    "make migrate",
    "make db-posture",
    "pytest",
    "image_smoke.sh",
    "browser_runner.sh",
    "tls_stack.sh",
)
MANDATORY_STEP_COUNT: Final = 3


def validate_repository(root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    """Validate every workflow and return authenticated provenance summaries."""
    provenance = _load_provenance(root / "ops/testing/action-provenance.json")
    workflow_root = root / ".github/workflows"
    paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
    if not paths:
        _fail("no hosted workflow is tracked")
    for path in paths:
        _validate_workflow(path.read_text(encoding="utf-8"), path.name)
    return provenance


def _validate_workflow(source: str, name: str) -> None:
    if re.search(r"^\s*services:\s*$", source, re.MULTILINE):
        _fail(f"{name} declares forbidden services")
    lines = source.splitlines()
    for line in lines:
        if "uses:" not in line:
            continue
        match = USE.fullmatch(line)
        if match is None or ACTION_MAP.get(match.group(1)) != (
            match.group(2),
            match.group(3),
        ):
            _fail(f"{name} contains an unknown action or release comment")
    for job_name, block in _job_blocks(lines):
        _validate_job(job_name, block)


def _job_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    jobs: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current: list[str] = []
    in_jobs = False
    for line in lines:
        if line == "jobs:":
            in_jobs = True
            continue
        match = JOB.fullmatch(line) if in_jobs else None
        if match is not None:
            if current_name is not None:
                jobs.append((current_name, current))
            current_name, current = match.group(1), []
        elif current_name is not None:
            current.append(line)
    if current_name is not None:
        jobs.append((current_name, current))
    if not jobs:
        _fail("workflow has no hosted jobs")
    return jobs


def _validate_job(name: str, block: list[str]) -> None:
    steps = _steps(block)
    if len(steps) < MANDATORY_STEP_COUNT:
        _fail(f"job {name} lacks mandatory bootstrap steps")
    expected = ("actions/checkout", "astral-sh/setup-uv")
    for index, owner in enumerate(expected):
        uses = _field(steps[index], "uses")
        if uses is None or not uses.startswith(f"{owner}@"):
            _fail(f"job {name} does not start with checkout and setup-uv")
        if _field(steps[index], "run") is not None:
            _fail(f"job {name} runs a command before snapshot")
    run_steps = [step for step in steps if _field(step, "run") is not None]
    if not run_steps or _field(run_steps[0], "run") != SNAPSHOT:
        _fail(f"job {name} does not snapshot as its first command")
    joined = "\n".join("\n".join(step) for step in steps)
    first_consumer = min(
        (joined.find(token) for token in DATABASE_CONSUMERS if token in joined),
        default=-1,
    )
    if first_consumer >= 0:
        startup = joined.find("ops/testing/ci_postgres.sh up")
        if startup < 0 or startup > first_consumer:
            _fail(f"job {name} consumes PostgreSQL before claimed startup")


def _steps(block: list[str]) -> list[list[str]]:
    steps: list[list[str]] = []
    current: list[str] = []
    in_steps = False
    for line in block:
        if line == "    steps:":
            in_steps = True
            continue
        if in_steps and STEP.match(line):
            if current:
                steps.append(current)
            current = [line]
        elif in_steps and current:
            current.append(line)
    if current:
        steps.append(current)
    return steps


def _field(step: list[str], field: str) -> str | None:
    marker = f"{field}:"
    for line in step:
        stripped = line.strip().removeprefix("- ")
        if stripped.startswith(marker):
            value = stripped.removeprefix(marker).strip()
            return (
                value
                if value not in {"|", ">", ">-"}
                else "\n".join(
                    item.strip() for item in step[1:] if item.startswith("          ")
                )
            )
    return None


def _load_provenance(path: Path) -> tuple[str, ...]:
    value: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"schema_version", "actions"}:
        _fail("action provenance has an open root")
    if value.get("schema_version") != 1:
        _fail("action provenance version is invalid")
    actions = value.get("actions")
    if not isinstance(actions, list) or len(actions) != len(ACTION_MAP):
        _fail("action provenance does not contain the closed action set")
    observed: dict[str, tuple[str, str]] = {}
    summaries: list[str] = []
    for item in actions:
        if not isinstance(item, dict):
            _fail("action provenance entry is invalid")
        repository = item.get("repository")
        commit, tag = item.get("commit"), item.get("tag")
        if not isinstance(repository, str) or not repository.startswith(
            "https://github.com/"
        ):
            _fail("action provenance repository is invalid")
        action = repository.removeprefix("https://github.com/")
        if not isinstance(commit, str) or not isinstance(tag, str):
            _fail("action provenance identity is invalid")
        observed[action] = (commit, tag)
        summaries.append(f"{action} {tag} {commit}")
    if observed != ACTION_MAP:
        _fail("action provenance differs from the approved release map")
    setup = actions[1]
    if not isinstance(setup, dict) or setup.get("uv_version") != "0.11.28":
        _fail("setup-uv provenance lacks the configured uv version")
    return tuple(summaries)


def _fail(message: str) -> Never:
    raise IsolationError(message)


def main() -> int:
    """Validate repository workflows and print release-tag provenance."""
    try:
        for summary in validate_repository():
            sys.stdout.write(f"{summary}\n")
    except (IsolationError, OSError, ValueError) as error:
        sys.stderr.write(f"action-pins: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
