"""Inspect and mutate only a reauthenticated runner selector union."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import IsolationError, JsonValue
from ops.testing.isolation_runner_create import runner_create_arguments
from ops.testing.isolation_runner_docker_inspection import (
    RunnerContainer,
    inspect_runner_containers,
)
from ops.testing.isolation_runner_validation import (
    select_runner_candidate,
    validate_runner_config,
)

if TYPE_CHECKING:
    from ops.testing.isolation_docker_metadata import CommandRunner
    from ops.testing.isolation_runner_authority import RunnerAuthority

HEX64 = re.compile(r"^[0-9a-f]{64}$")
STOPPABLE_STATES = frozenset({"paused", "restarting", "running"})
REMOVABLE_STATES = STOPPABLE_STATES | {"created", "dead", "exited"}


def select_runner(
    run: CommandRunner,
    authority: RunnerAuthority,
    *,
    expected_id: str | None = None,
) -> RunnerContainer | None:
    """Read and select the exact ID/name/intent-label union."""
    recorded = _optional_text(
        authority.creation.get("container_id"),
        "runner container ID",
    )
    return select_runner_candidate(
        inspect_runner_containers(run),
        expected_id=recorded if expected_id is None else expected_id,
        expected_name=_text(
            authority.creation.get("container_name"),
            "runner container name",
        ),
        expected_labels=authority.labels,
    )


def create_candidate(
    run: CommandRunner,
    authority: RunnerAuthority,
) -> RunnerContainer:
    """Execute the intent-derived argv and reselect its exact returned ID."""
    creation = authority.creation
    arguments = runner_create_arguments(
        authority.ledger,
        authority.claim,
        authority.service,
        _text(creation.get("container_name"), "runner container name"),
        _text(creation.get("intent_sha256"), "runner intent hash"),
    )
    output = run(arguments).strip()
    if HEX64.fullmatch(output) is None:
        _fail("Docker create returned an invalid runner container ID")
    candidate = select_runner(run, authority, expected_id=output)
    if candidate is None:
        _fail("created runner is absent from the selector union")
    return candidate


def remove_candidate(run: CommandRunner, authority: RunnerAuthority) -> None:
    """Reinspect before stop/remove and leave only authenticated absence."""
    candidate = select_runner(run, authority)
    if candidate is None:
        return
    validate_runner_config(candidate, authority.service)
    require_removable_state(candidate)
    if candidate.state in STOPPABLE_STATES:
        run(("container", "stop", "--time", "10", candidate.identifier))
        candidate = select_runner(run, authority)
        if candidate is None:
            return
        validate_runner_config(candidate, authority.service)
        if candidate.state in STOPPABLE_STATES:
            _fail("runner remained live after Docker stop")
    run(("container", "rm", candidate.identifier))


def never_started(candidate: RunnerContainer) -> bool:
    """Return whether Docker proves the container has never executed."""
    return (
        candidate.state == "created"
        and candidate.pid == 0
        and candidate.restart_count == 0
    )


def require_removable_state(candidate: RunnerContainer) -> None:
    """Reject daemon states outside the closed cleanup transition set."""
    if candidate.state not in REMOVABLE_STATES:
        _fail("runner container state is not cleanup-authorized")


def _optional_text(value: JsonValue, context: str) -> str | None:
    if value is not None and not isinstance(value, str):
        _fail(f"{context} must be a string or null")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
