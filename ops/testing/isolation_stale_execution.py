"""Execute one identity-bound changed-boot physical action."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

import rfc8785

from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_stale_candidate_execution import (
    execute_candidate_publication,
)
from ops.testing.isolation_stale_docker_execution import execute_stale_docker_action
from ops.testing.isolation_stale_filesystem_execution import remove_stale_staging
from ops.testing.isolation_stale_process_execution import (
    require_prior_processes_absent,
)

if TYPE_CHECKING:
    from ops.testing.isolation_docker_metadata import CommandRunner


@dataclass(frozen=True, slots=True)
class StaleExecutionAdapters:
    """Inject read-only procfs and Docker command boundaries for verification."""

    proc_root: Path | None = None
    docker_runner: CommandRunner | None = None


def execute_stale_action(
    action: JsonObject,
    identity: JsonObject,
    *,
    previous_boot_id: str,
    current_boot_id: str,
    adapters: StaleExecutionAdapters | None = None,
) -> None:
    """Apply only the physical operation authenticated by one action hash."""
    _validate_binding(action, identity)
    if previous_boot_id == current_boot_id:
        _fail("stale action requires changed-boot authority")
    boundaries = StaleExecutionAdapters() if adapters is None else adapters
    operation = action.get("operation")
    if operation == "observe-prior-boot-process-absent":
        root = Path("/proc") if boundaries.proc_root is None else boundaries.proc_root
        require_prior_processes_absent(identity, root)
        return
    if operation == "remove-staging":
        remove_stale_staging(identity)
        return
    if operation in {
        "publish-or-adopt-candidate-envelope",
        "publish-or-adopt-candidate-history",
    }:
        execute_candidate_publication(identity)
        return
    if operation in {
        "stop-remove-container",
        "detach-borrowed-network",
        "remove-owned-network",
        "remove-owned-volume",
    }:
        execute_stale_docker_action(identity, boundaries.docker_runner)
        return
    _fail("stale physical action adapter is not implemented")


def _validate_binding(action: JsonObject, identity: JsonObject) -> None:
    digest = hashlib.sha256(rfc8785.dumps(identity)).hexdigest()
    if digest != action.get("identity_sha256"):
        _fail("stale action identity hash drifted")
    for key in ("action_id", "claim_id", "operation", "resource_kind"):
        if identity.get(key) != action.get(key):
            _fail("stale action summary differs from its full identity")


def _fail(message: str) -> Never:
    raise IsolationError(message)
