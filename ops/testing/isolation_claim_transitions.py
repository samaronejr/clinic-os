"""Apply stable-lock serialized isolation claim state transitions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Never

from ops.testing.isolation_candidate_contract import contract_for_purpose
from ops.testing.isolation_candidate_release import release_candidate_claim
from ops.testing.isolation_claim_records import (
    build_reserved_claim,
    claim_objects,
    load_claim_spec,
    validate_candidate_claim_root,
    validate_claim_id,
    validate_dependencies,
    validate_existing_dependencies,
)
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    ensure_private_directory,
    utc_now,
)
from ops.testing.isolation_filesystem_claim import (
    load_filesystem_observation,
    require_filesystem_release_ready,
)
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_process_claim import validate_process_borrowed_dependencies
from ops.testing.isolation_process_observation import (
    load_process_observation,
    require_process_release_ready,
)
from ops.testing.isolation_runner_state import (
    require_removed_runner_release_ready,
    runner_is_prepared_for_activation,
)
from ops.testing.isolation_stack_claim import validate_stack_collisions
from ops.testing.isolation_stack_observation import (
    load_stack_observation,
    require_stack_release_ready,
)


def _fail(message: str) -> Never:
    raise IsolationError(message)


def reserve_claim(ledger_path: Path, spec_path: Path) -> str:
    """Reserve one closed claim before creating its private mutable root."""
    spec = load_claim_spec(spec_path)
    claim_id = spec["claim_id"]
    if not isinstance(claim_id, str):
        _fail("claim ID is not textual")
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        validate_dependencies(spec, claims)
        if spec["kind"] == "stack":
            validate_stack_collisions(spec, session.ledger, claims)
        elif spec["kind"] == "process":
            validate_process_borrowed_dependencies(spec, claims)
        attempt_root = Path(str(session.ledger["attempt_root"]))
        validate_candidate_claim_root(spec, attempt_root)
        claims_root = attempt_root / "claims"
        claim_root = claims_root / claim_id
        try:
            os.lstat(claim_root)
        except FileNotFoundError:
            pass
        else:
            _fail("claim root already exists before reservation")
        timestamp = utc_now()
        claims.append(build_reserved_claim(spec, timestamp))
        claims.sort(key=lambda item: str(item["claim_id"]))
        session.ledger["last_verified_at_utc"] = timestamp
        session.commit()
        ensure_private_directory(claims_root)
        ensure_private_directory(claim_root)
    return claim_id


def activate_claim(ledger_path: Path, claim_id: str, observed_path: Path) -> None:
    """Advance one reserved claim after its exact fresh observation validates."""
    identifier = validate_claim_id(claim_id)
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = _find_claim(claims, identifier)
        prepared_runner = runner_is_prepared_for_activation(claim)
        if claim.get("status") != "reserved" and not prepared_runner:
            _fail("claim is not reserved")
        if claim.get("runner_creation") is not None and not prepared_runner:
            _fail("direct activation cannot carry runner creation state")
        validate_existing_dependencies(claim, claims)
        claim_root = Path(str(session.ledger["attempt_root"])) / str(
            claim["root_relative_path"]
        )
        kind = claim.get("kind")
        if kind == "filesystem":
            observed = load_filesystem_observation(observed_path, claim, claim_root)
        elif kind == "stack":
            observed = load_stack_observation(observed_path, claim)
        elif kind == "process":
            observed = load_process_observation(observed_path, claim, claim_root)
        else:
            _fail("claim kind does not support direct activation")
        timestamp = utc_now()
        previous = (
            claim.get("prepared_at_utc")
            if prepared_runner
            else claim.get("reserved_at_utc")
        )
        if not isinstance(previous, str) or timestamp <= previous:
            _fail("activation timestamp does not follow prior lifecycle state")
        claim["activated_at_utc"] = timestamp
        claim["last_verified_at_utc"] = timestamp
        claim["observed"] = observed
        claim["status"] = "active"
        session.ledger["last_verified_at_utc"] = timestamp
        session.commit()


def release_claim(ledger_path: Path, claim_id: str) -> None:
    """Remove one dependency-free claim only after its mutable root is absent."""
    identifier = validate_claim_id(claim_id)
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = _find_claim(claims, identifier)
        if any(
            _depends_on(item.get("dependency_claim_ids"), identifier) for item in claims
        ):
            _fail("claim has a reverse dependency")
        runner_creation = claim.get("runner_creation")
        claim_root = Path(str(session.ledger["attempt_root"])) / str(
            claim["root_relative_path"]
        )
        kind = claim.get("kind")
        if contract_for_purpose(claim.get("purpose")) is not None:
            release_candidate_claim(session, claims, claim, claim_root)
            return
        if isinstance(runner_creation, dict):
            require_removed_runner_release_ready(claim, claim_root)
        elif kind == "filesystem":
            require_filesystem_release_ready(claim, claim_root)
        elif kind == "stack":
            require_stack_release_ready(claim, claim_root)
        elif kind == "process":
            require_process_release_ready(claim, claim_root)
        else:
            _fail("claim kind does not support direct release")
        claims.remove(claim)
        session.ledger["last_verified_at_utc"] = utc_now()
        session.commit()


def _find_claim(claims: list[JsonObject], claim_id: str) -> JsonObject:
    matches = [claim for claim in claims if claim.get("claim_id") == claim_id]
    if len(matches) != 1:
        _fail("claim identity is missing or duplicated")
    return matches[0]


def _depends_on(value: JsonValue, claim_id: str) -> bool:
    return isinstance(value, list) and claim_id in value
