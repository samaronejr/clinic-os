"""Own the final-gate decision staging filesystem claim."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from ops.testing.isolation_claim_transitions import (
    activate_claim,
    release_claim,
    reserve_claim,
)
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    directory_identity,
    write_no_replace,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_review_runtime_inputs import ReviewRuntimeInputs


def ensure_final_control_root(control_root: Path) -> None:
    """Create or authenticate the fixed private control and terminal roots."""
    terminal = control_root / "terminal"
    control_root.mkdir(mode=MODE_DIRECTORY, exist_ok=True)
    terminal.mkdir(mode=MODE_DIRECTORY, exist_ok=True)
    for path in (control_root, terminal):
        identity = directory_identity(path)
        if identity.get("mode") != MODE_DIRECTORY or path.resolve() != path:
            message = "final control root identity is invalid"
            raise IsolationError(message)


def activate_final_staging(runtime: ReviewRuntimeInputs, claim_id: str) -> Path:
    """Reserve and activate the exact final decision staging root."""
    spec: JsonObject = {
        "claim_id": claim_id,
        "dependency_claim_ids": [],
        "desired": {"owned_files": [], "published_outputs": []},
        "kind": "filesystem",
        "purpose": "final-gate-staging",
    }
    spec_path = runtime.controller_root / f"{claim_id}.final-spec.json"
    write_no_replace(spec_path, canonical_bytes(spec), mode=MODE_IMMUTABLE)
    try:
        _ = reserve_claim(runtime.ledger_path, spec_path)
    finally:
        spec_path.unlink(missing_ok=True)
    root = runtime.attempt_root / "claims" / claim_id
    try:
        (root / "staging").mkdir(mode=MODE_DIRECTORY)
        observation: JsonObject = {"owned_files": [], "published_outputs": []}
        observed_path = runtime.controller_root / f"{claim_id}.final-observation.json"
        write_no_replace(
            observed_path, canonical_bytes(observation), mode=MODE_IMMUTABLE
        )
        try:
            activate_claim(runtime.ledger_path, claim_id, observed_path)
        finally:
            observed_path.unlink(missing_ok=True)
    except Exception:
        if root.exists() and not root.is_symlink():
            shutil.rmtree(root)
        release_claim(runtime.ledger_path, claim_id)
        raise
    return root


def release_final_staging(
    runtime: ReviewRuntimeInputs, claim_id: str, root: Path
) -> None:
    """Remove and release only the recorded final-gate staging claim."""
    if root != runtime.attempt_root / "claims" / claim_id or root.is_symlink():
        message = "final staging root escaped its claim namespace"
        raise IsolationError(message)
    identity = directory_identity(root)
    if identity.get("mode") != MODE_DIRECTORY or root.resolve() != root:
        message = "final staging root identity is invalid"
        raise IsolationError(message)
    shutil.rmtree(root)
    release_claim(runtime.ledger_path, claim_id)
