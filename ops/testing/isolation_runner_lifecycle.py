"""Own same-boot Docker side effects for one browser-runner claim."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_claim_records import claim_objects, validate_claim_id
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.isolation_filesystem_refresh import remove_empty_claim_root
from ops.testing.isolation_ledger_store import LedgerSession, locked_open_ledger
from ops.testing.isolation_runner_authority import (
    RunnerAuthority,
    authenticate_runner_authority,
)
from ops.testing.isolation_runner_physical import (
    create_candidate,
    never_started,
    remove_candidate,
    require_removable_state,
    select_runner,
)
from ops.testing.isolation_runner_records import (
    begin_discard,
    find_runner_claim,
    finish_discard,
    prepare_runner,
    record_intent,
    runner_creation,
)
from ops.testing.isolation_runner_validation import (
    runner_created_observation,
    validate_runner_config,
)

if TYPE_CHECKING:
    from ops.testing.isolation_docker_metadata import CommandRunner
    from ops.testing.isolation_runner_docker_inspection import RunnerContainer


def create_runner(
    ledger_path: Path,
    claim_id: str,
    docker_runner: CommandRunner | None = None,
) -> str:
    """Create or adopt only the exact journal-authorized inert runner."""
    identifier = validate_claim_id(claim_id)
    run = run_docker_command if docker_runner is None else docker_runner
    unsafe = False
    result = ""
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = find_runner_claim(claims, identifier)
        _ensure_create_intent(session, claims, claim)
        authority = authenticate_runner_authority(session.ledger, claim)
        candidate = select_runner(run, authority)
        if claim.get("status") == "prepared":
            return _prepared_replay(candidate, authority)
        if (
            claim.get("status") != "reserved"
            or authority.creation.get("state") != "intent"
        ):
            _fail("runner claim is not at a create or prepared replay state")
        candidate = create_candidate(run, authority) if candidate is None else candidate
        validate_runner_config(candidate, authority.service)
        if never_started(candidate):
            observed = runner_created_observation(candidate, claim)
            result = prepare_runner(session, claims, claim, observed)
        else:
            _discard_locked(
                session,
                authority,
                "unsafe-state",
                candidate,
                run,
            )
            unsafe = True
    if unsafe:
        _fail("matching intended runner prematurely started")
    return result


def discard_runner(
    ledger_path: Path,
    claim_id: str,
    reason: str,
    docker_runner: CommandRunner | None = None,
) -> None:
    """Journal before removing only the exact same-boot runner identity."""
    identifier = validate_claim_id(claim_id)
    run = run_docker_command if docker_runner is None else docker_runner
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = find_runner_claim(claims, identifier)
        creation = runner_creation(claim)
        if creation.get("creation_boot_id") != session.ledger.get("boot_id"):
            _fail("runner creation belongs to a different boot")
        authority = authenticate_runner_authority(session.ledger, claim)
        candidate = select_runner(run, authority)
        if candidate is not None:
            validate_runner_config(candidate, authority.service)
            require_removable_state(candidate)
        _discard_locked(session, authority, reason, candidate, run)


def _ensure_create_intent(
    session: LedgerSession,
    claims: list[JsonObject],
    claim: JsonObject,
) -> None:
    if claim.get("runner_creation") is None:
        record_intent(session, claims, claim)
        return
    creation = runner_creation(claim)
    if creation.get("creation_boot_id") != session.ledger.get("boot_id"):
        _fail("runner creation belongs to a different boot")


def _prepared_replay(
    candidate: RunnerContainer | None,
    authority: RunnerAuthority,
) -> str:
    if authority.creation.get("state") != "prepared" or candidate is None:
        _fail("prepared runner identity is absent")
    validate_runner_config(candidate, authority.service)
    if not never_started(candidate):
        _fail("prepared runner no longer has its inert created state")
    observed = runner_created_observation(candidate, authority.claim)
    if observed != authority.claim.get("observed"):
        _fail("prepared runner observation drifted")
    return candidate.identifier


def _discard_locked(
    session: LedgerSession,
    authority: RunnerAuthority,
    reason: str,
    candidate: RunnerContainer | None,
    run: CommandRunner,
) -> None:
    claim = authority.claim
    creation = authority.creation
    claims = claim_objects(session.ledger["claims"])
    if creation.get("state") != "removed":
        captured = None if candidate is None else candidate.identifier
        begin_discard(session, claims, claim, reason, captured)
        remove_candidate(run, authority)
        if select_runner(run, authority) is not None:
            _fail("runner selector union remains after cleanup")
        finish_discard(session, claim)
    elif candidate is not None:
        _fail("removed runner selector union was recreated")
    claim_root = Path(str(session.ledger["attempt_root"])) / str(
        claim["root_relative_path"]
    )
    remove_empty_claim_root(claim_root)
    claims.remove(claim)
    session.commit()


def _fail(message: str) -> Never:
    raise IsolationError(message)
