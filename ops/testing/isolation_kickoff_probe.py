"""Run and authenticate the mandatory kickoff capability probe."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.cgroup_capability_probe import ProbeRequest
from ops.testing.cgroup_probe_records import _validate_completed
from ops.testing.isolation_common import IsolationError, JsonObject

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def run_kickoff_probe(
    attempt_root: Path,
    proof_path: Path,
    proof: JsonObject,
    probe_runner: Callable[[ProbeRequest], Path],
) -> Path:
    """Publish or replay the exact authenticated todo1-kickoff journal."""
    proof_sha = proof.get("sha256")
    if not isinstance(proof_sha, str):
        _fail("execution-host proof record lacks its SHA-256")
    request = ProbeRequest(
        attempt_id=attempt_root.name,
        attempt_root=attempt_root,
        proof_path=proof_path,
        proof_sha256=proof_sha,
        purpose="todo1-kickoff",
    )
    expected = attempt_root / "execution-host-probes/todo1-kickoff.json"
    if probe_runner(request) != expected:
        _fail("kickoff capability probe returned a noncanonical journal")
    _validate_completed(expected, request)
    if {path.name for path in expected.parent.iterdir()} != {expected.name}:
        _fail("kickoff capability-probe directory contains an unknown entry")
    return expected


def _fail(message: str) -> Never:
    raise IsolationError(message)
