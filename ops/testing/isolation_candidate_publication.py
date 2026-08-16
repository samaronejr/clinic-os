"""Own the candidate envelope bind-before-publication transition."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final, Never

import rfc8785

from ops.testing.isolation_atomic_publication import (
    publish_immutable,
    require_published_bytes,
)
from ops.testing.isolation_candidate_contract import (
    CandidateContract,
    contract_for_purpose,
    envelope_binding,
    validate_candidate_desired,
    validate_candidate_envelope,
)
from ops.testing.isolation_candidate_records import (
    authorization_destination,
    bound_envelope_bytes,
    candidate_observations,
    object_value,
    object_values,
    published_observation,
    text_value,
    unpublished_observation,
)
from ops.testing.isolation_candidate_verification import verify_candidate_image
from ops.testing.isolation_claim_records import (
    claim_objects,
    validate_claim_id,
    validate_existing_dependencies,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    ensure_private_directory,
    load_json,
    regular_identity,
    utc_now,
)
from ops.testing.isolation_ledger_store import LedgerSession, locked_open_ledger

TIMESTAMP: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)


type _CandidateVerifier = Callable[[JsonObject, JsonObject, JsonObject], None]
type _CandidatePublisher = Callable[[Path, bytes, str], None]


def _fail(message: str) -> Never:
    raise IsolationError(message)


def publish_candidate_envelope(
    ledger_path: Path,
    claim_id: str,
    staged_path: Path,
    *,
    verifier: _CandidateVerifier | None = None,
    publisher: _CandidatePublisher | None = None,
) -> bytes:
    """Bind then publish one canonical candidate envelope under the stable lock."""
    identifier = validate_claim_id(claim_id)
    verify = verify_candidate_image if verifier is None else verifier
    publish = publish_immutable if publisher is None else publisher
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        claim = _claim(claims, identifier)
        contract = contract_for_purpose(claim.get("purpose"))
        if contract is None or claim.get("kind") != "filesystem":
            _fail("claim is not a candidate publisher")
        if claim.get("status") != "active":
            _fail("candidate publisher is not active")
        validate_existing_dependencies(claim, claims)
        attempt_root = Path(text_value(session.ledger["attempt_root"], "attempt root"))
        desired = object_value(claim["desired"], "candidate desired")
        validate_candidate_desired(desired, identifier, contract, attempt_root)
        expected_staged = attempt_root / str(claim["root_relative_path"])
        expected_staged /= "candidate-envelope.json"
        if not staged_path.is_absolute() or staged_path != expected_staged:
            _fail("staged candidate envelope path is not claim-owned")
        authorizations = object_values(
            desired["published_outputs"], "candidate authorizations"
        )
        observations = candidate_observations(claim, authorizations)
        raw = _load_or_bind(
            session,
            claim,
            contract,
            staged_path,
            verify,
        )
        if observations[1] != unpublished_observation(authorizations[1]):
            _fail("candidate history cannot precede envelope completion")
        authorization = authorizations[0]
        destination = authorization_destination(authorization)
        expected_observation = published_observation(authorization, raw)
        if observations[0].get("status") == "published":
            if observations[0] != expected_observation:
                _fail("published candidate envelope observation drifted")
            require_published_bytes(destination, raw)
            return raw
        if observations[0] != unpublished_observation(authorization):
            _fail("candidate envelope authorization state is invalid")
        root = Path(text_value(authorization["root_path"], "authorization root"))
        _ensure_destination_parent(destination, root)
        publish(destination, raw, identifier)
        observations[0].clear()
        observations[0].update(expected_observation)
        now = utc_now()
        claim["last_verified_at_utc"] = now
        session.ledger["last_verified_at_utc"] = now
        session.commit()
        return raw


def _load_or_bind(
    session: LedgerSession,
    claim: JsonObject,
    contract: CandidateContract,
    staged_path: Path,
    verifier: _CandidateVerifier,
) -> bytes:
    desired = object_value(claim["desired"], "candidate desired")
    authorizations = object_values(
        desired["published_outputs"], "candidate authorizations"
    )
    binding = claim.get("candidate_envelope_binding")
    if binding is not None:
        envelope, raw = bound_envelope_bytes(binding, session.ledger, claim, contract)
        if _exists(staged_path):
            staged_envelope, staged_raw = _load_staged(staged_path)
            if staged_envelope != envelope or staged_raw != raw:
                _fail("staged candidate envelope drifted after binding")
        return raw
    _require_unbound_destinations_absent(authorizations)
    envelope, raw = _load_staged(staged_path)
    validate_candidate_envelope(envelope, session.ledger, claim, contract)
    now = utc_now()
    validate_candidate_publication_time(envelope, claim.get("activated_at_utc"), now)
    verifier(session.ledger, claim, envelope)
    claim["candidate_envelope_binding"] = envelope_binding(envelope)
    claim["last_verified_at_utc"] = now
    session.ledger["last_verified_at_utc"] = now
    session.commit()
    return raw


def _load_staged(path: Path) -> tuple[JsonObject, bytes]:
    if path.is_symlink():
        _fail("staged candidate envelope is a symlink")
    regular_identity(path, mode=MODE_IMMUTABLE)
    envelope, raw = load_json(path)
    if raw != rfc8785.dumps(envelope) + b"\n":
        _fail("staged candidate envelope is not RFC 8785 canonical")
    return envelope, raw


def _require_unbound_destinations_absent(authorizations: list[JsonObject]) -> None:
    if any(_exists(authorization_destination(item)) for item in authorizations):
        _fail("binding-null destination is corruption")


def validate_candidate_publication_time(
    envelope: JsonObject, activated: JsonValue, current: JsonValue
) -> None:
    """Require one canonical publication timestamp inside its bound interval."""
    published = envelope.get("published_at_utc")
    if (
        not isinstance(published, str)
        or TIMESTAMP.fullmatch(published) is None
        or not isinstance(activated, str)
        or not isinstance(current, str)
        or TIMESTAMP.fullmatch(activated) is None
        or TIMESTAMP.fullmatch(current) is None
        or published < activated
        or published > current
    ):
        _fail("candidate publication timestamp is outside the active lock interval")


def _ensure_destination_parent(destination: Path, root: Path) -> None:
    ensure_private_directory(root)
    if destination.parent != root:
        ensure_private_directory(destination.parent)


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _claim(claims: list[JsonObject], claim_id: str) -> JsonObject:
    matches = [claim for claim in claims if claim.get("claim_id") == claim_id]
    if len(matches) != 1:
        _fail("candidate claim identity is missing or duplicated")
    return matches[0]
