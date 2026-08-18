"""Complete or recover review publication, cleanup, and terminal sealing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ops.testing.isolation_common import (
    IsolationError,
    JsonValue,
    canonical_bytes,
    raw_sha256,
    utc_now,
)
from ops.testing.isolation_review_failure import publish_review_failure
from ops.testing.isolation_review_filesystems import (
    ReviewFilesystems,
    cleanup_review_filesystems,
)
from ops.testing.isolation_review_lane_controller import (
    ReviewLaneSession,
    ReviewSeal,
    ToolClosure,
)
from ops.testing.isolation_review_lane_record import require_successful_review_history
from ops.testing.isolation_review_process import recover_recorded_process
from ops.testing.isolation_terminal_publication import publish_terminal_file
from ops.testing.validate_review_verdict import validate_review_verdict

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_review_runtime_inputs import ReviewRuntimeInputs


@dataclass(frozen=True, slots=True)
class ReviewRecoveryRequest:
    """Closed lane and journal identities needed for recovery completion."""

    lane: str
    sha: str
    review_id: str
    private_id: str | None
    journal: Path


def review_approved(lane: str, sha: str, filesystems: ReviewFilesystems) -> bool:
    """Validate the staged lane verdict and return exact approval semantics."""
    path = filesystems.review_root / f"staging/{lane}-verdict.json"
    validated = validate_review_verdict(path.read_bytes(), lane, sha)
    value = json.loads(validated)
    return isinstance(value, dict) and value.get("verdict") == "APPROVE"


def publish_review_success(
    runtime: ReviewRuntimeInputs,
    lane: str,
    filesystems: ReviewFilesystems,
) -> str:
    """Replay or publish the exact ordered lane authorization prefix."""
    staged = filesystems.review_root / "staging"
    publications = (
        (
            ("f1-verdict", staged / "F1-verdict.json"),
            ("f1-receipt", staged / "review-runner.json"),
        )
        if lane == "F1"
        else (
            ("f2-prerequisites", staged / "F2-prerequisites.json"),
            ("f2-verdict", staged / "F2-verdict.json"),
            ("f2-receipt", staged / "review-runner.json"),
        )
    )
    hashes: list[JsonValue] = [
        publish_terminal_file(
            runtime.ledger_path,
            runtime.publisher_claim_id,
            authorization,
            source,
            runtime.terminal_root,
        )
        for authorization, source in publications
    ]
    return raw_sha256(canonical_bytes(hashes))


def recover_review_lane(
    session: ReviewLaneSession,
    runtime: ReviewRuntimeInputs,
    request: ReviewRecoveryRequest,
) -> int:
    """Fail closed or replay a fully successful interrupted publication."""
    current = session.record.get("child_process_claim_id")
    current_claim = str(current) if isinstance(current, str) else None
    recover_recorded_process(runtime, current_claim)
    codex_sha = session.record.get("codex_tool_sha256")
    uv_sha = session.record.get("uv_tool_sha256")
    filesystems = ReviewFilesystems(
        request.review_id,
        request.private_id,
        runtime.attempt_root / "claims" / request.review_id,
        (
            runtime.attempt_root / "claims" / request.private_id
            if request.private_id is not None
            else None
        ),
        ToolClosure(
            str(codex_sha) if isinstance(codex_sha, str) else runtime.codex_sha256,
            str(uv_sha) if isinstance(uv_sha, str) else None,
        ),
    )
    success = False
    try:
        require_successful_review_history(session.record)
        success = review_approved(request.lane, request.sha, filesystems)
    except (IsolationError, FileNotFoundError, ValueError, json.JSONDecodeError):
        success = False
    terminal_digest = (
        publish_review_success(runtime, request.lane, filesystems) if success else None
    )
    cleanup_review_filesystems(runtime, filesystems)
    session.seal(ReviewSeal(success, utc_now(), terminal_digest, terminal_digest))
    if not success:
        _ = publish_review_failure(
            runtime.ledger_path,
            request.journal,
            request.lane,
            "io-failure",
            runtime.lineage_validation_sha256,
        )
    return 0 if success else 2
