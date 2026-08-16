"""Complete or abort a candidate claim without bypassing publication history."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Never

import rfc8785

from ops.testing.isolation_atomic_publication import (
    publish_immutable,
    require_published_bytes,
)
from ops.testing.isolation_candidate_contract import (
    contract_for_purpose,
    validate_candidate_desired,
)
from ops.testing.isolation_candidate_records import (
    authorization_destination,
    bound_envelope_bytes,
    candidate_history_record,
    candidate_observations,
    object_value,
    object_values,
    published_observation,
    text_value,
    unpublished_observation,
)
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    ensure_private_directory,
    utc_now,
)

if TYPE_CHECKING:
    from ops.testing.isolation_ledger_store import LedgerSession


def _fail(message: str) -> Never:
    raise IsolationError(message)


def release_candidate_claim(
    session: LedgerSession,
    claims: list[JsonObject],
    claim: JsonObject,
    claim_root: Path,
) -> bool:
    """Publish the exact history successor before removing a bound publisher."""
    contract = contract_for_purpose(claim.get("purpose"))
    if contract is None:
        return False
    desired = object_value(claim["desired"], "candidate desired")
    attempt_root = Path(text_value(session.ledger["attempt_root"], "attempt root"))
    claim_id = text_value(claim["claim_id"], "candidate claim ID")
    validate_candidate_desired(desired, claim_id, contract, attempt_root)
    authorizations = object_values(
        desired["published_outputs"], "candidate authorizations"
    )
    binding = claim.get("candidate_envelope_binding")
    if binding is None:
        _release_unbound(session, claims, claim, claim_root, authorizations)
        return True
    if claim.get("status") != "active":
        _fail("bound candidate publisher is not active")
    observations = candidate_observations(claim, authorizations)
    _, envelope_raw = bound_envelope_bytes(binding, session.ledger, claim, contract)
    expected_envelope = published_observation(authorizations[0], envelope_raw)
    if observations[0] != expected_envelope:
        _fail("candidate envelope must be published before history")
    require_published_bytes(authorization_destination(authorizations[0]), envelope_raw)
    _require_absent(claim_root, "candidate context or staging remains")
    binding_object = object_value(binding, "candidate envelope binding")
    history = candidate_history_record(
        session.ledger,
        claim,
        binding_object,
        authorizations,
        observations[0],
    )
    history_raw = rfc8785.dumps(history) + b"\n"
    expected_history = published_observation(authorizations[1], history_raw)
    history_destination = authorization_destination(authorizations[1])
    if observations[1] == unpublished_observation(authorizations[1]):
        history_root = Path(text_value(authorizations[1]["root_path"], "history root"))
        ensure_private_directory(history_root)
        publish_immutable(history_destination, history_raw, claim_id)
        observations[1].clear()
        observations[1].update(expected_history)
        _checkpoint(session, claim)
    elif observations[1] != expected_history:
        _fail("candidate history observation drifted")
    require_published_bytes(history_destination, history_raw)
    claims.remove(claim)
    session.ledger["last_verified_at_utc"] = utc_now()
    session.commit()
    return True


def _release_unbound(
    session: LedgerSession,
    claims: list[JsonObject],
    claim: JsonObject,
    claim_root: Path,
    authorizations: list[JsonObject],
) -> None:
    status = claim.get("status")
    observed = object_value(claim["observed"], "candidate observed")
    if status == "reserved":
        expected: JsonObject = {"owned_files": [], "published_outputs": []}
        if observed != expected:
            _fail("reserved candidate abort has observations")
    elif status == "active":
        outputs = candidate_observations(claim, authorizations)
        if any(
            output != unpublished_observation(authorization)
            for output, authorization in zip(outputs, authorizations, strict=True)
        ):
            _fail("active candidate abort has published state")
    else:
        _fail("candidate abort status is invalid")
    if any(_exists(authorization_destination(item)) for item in authorizations):
        _fail("unbound candidate abort found a destination")
    _require_absent(claim_root, "candidate abort staging remains")
    claims.remove(claim)
    session.ledger["last_verified_at_utc"] = utc_now()
    session.commit()


def _checkpoint(session: LedgerSession, claim: JsonObject) -> None:
    now = utc_now()
    claim["last_verified_at_utc"] = now
    session.ledger["last_verified_at_utc"] = now
    session.commit()


def _require_absent(path: Path, message: str) -> None:
    if _exists(path):
        _fail(message)


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True
