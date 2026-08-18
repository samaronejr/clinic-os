"""Construct closed review-stage payloads, environments, and deadlines."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ops.testing.isolation_review_filesystems import ReviewFilesystems
    from ops.testing.isolation_review_runtime_inputs import ReviewRuntimeInputs

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def stage_payload(
    runtime: ReviewRuntimeInputs,
    lane: str,
    sha: str,
    filesystems: ReviewFilesystems,
    stage: str,
) -> tuple[str, ...]:
    """Return the exact allowlisted payload for one journal stage."""
    if stage == "environment-sync":
        return (
            str(runtime.uv_path),
            "sync",
            "--frozen",
            "--project",
            str(runtime.worktree),
        )
    if stage == "prerequisites":
        return (
            str(PROJECT_ROOT / "ops/testing/f2_gate.sh"),
            "--sha",
            sha,
            "--evidence",
            str(filesystems.review_root / "staging/F2-prerequisites.json"),
        )
    return (
        str(PROJECT_ROOT / "ops/testing/run_review_gate.sh"),
        lane,
        sha,
        str(filesystems.review_root / f"staging/{lane}-verdict.json"),
        "1800",
    )


def stage_environment(
    runtime: ReviewRuntimeInputs,
    lane: str,
    filesystems: ReviewFilesystems,
    stage: str,
) -> dict[str, str]:
    """Return the exact empty-environment replacement for one stage."""
    if lane == "F2" and filesystems.private_root is not None:
        root = filesystems.private_root / "f2-private"
        environment = {
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": f"{root}/venv/bin:{root}/tool-bin:/usr/bin:/bin",
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_LINK_MODE": "copy",
            "UV_NO_CONFIG": "1",
            "UV_NO_PROGRESS": "1",
            "UV_PROJECT_ENVIRONMENT": str(root / "venv"),
            "UV_PYTHON_DOWNLOADS": "never",
        }
    else:
        environment = {
            "HOME": str(filesystems.review_root / "codex-home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "UV_PROJECT_ENVIRONMENT": str(runtime.worktree / ".venv"),
        }
    if stage == "review":
        environment["CLINIC_REVIEW_WORKSPACE_CLAIM_ID"] = filesystems.review_claim_id
    return environment


def stage_dependencies(filesystems: ReviewFilesystems) -> tuple[str, ...]:
    """Return the sorted active filesystem dependencies for a stage claim."""
    values = [filesystems.review_claim_id]
    if filesystems.private_claim_id is not None:
        values.append(filesystems.private_claim_id)
    return tuple(sorted(values))


def stage_deadline(stage: str) -> int:
    """Return the closed per-stage deadline in seconds."""
    return 1800 if stage in {"prerequisites", "review"} else 600
