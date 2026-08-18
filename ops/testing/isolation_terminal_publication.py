"""Import claim-staged bytes through one terminal publisher authorization."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_atomic_publication import (
    publish_immutable,
    require_published_bytes,
)
from ops.testing.isolation_candidate_records import (
    authorization_destination,
    published_observation,
)
from ops.testing.isolation_claim_records import claim_objects
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_terminal_publisher_authorizations import (
    entry_present,
    validate_terminal_publisher_claim,
)

if TYPE_CHECKING:
    from pathlib import Path


def publish_terminal_file(
    ledger_path: Path,
    publisher_claim_id: str,
    authorization_id: str,
    staged_path: Path,
    terminal_root: Path,
) -> str:
    """Publish one immutable staged file and persist its exact observation."""
    regular_identity(staged_path, mode=MODE_IMMUTABLE)
    raw = staged_path.read_bytes()
    with locked_open_ledger(ledger_path) as session:
        claims = claim_objects(session.ledger["claims"])
        publisher = _publisher(claims, publisher_claim_id)
        _ = validate_terminal_publisher_claim(publisher, terminal_root)
        authorizations = _objects(_object(publisher["desired"])["published_outputs"])
        observations = _objects(_object(publisher["observed"])["published_outputs"])
        indexes = [
            index
            for index, item in enumerate(authorizations)
            if item.get("authorization_id") == authorization_id
        ]
        if len(indexes) != 1:
            _fail("terminal authorization identity is missing or duplicated")
        index = indexes[0]
        authorization = authorizations[index]
        destination = authorization_destination(authorization)
        if destination.parent != terminal_root:
            _fail("terminal authorization destination escaped its root")
        expected = published_observation(authorization, raw)
        current = observations[index]
        if current.get("status") == "published":
            if current != expected:
                _fail("terminal publication replay drifted")
            require_published_bytes(destination, raw)
            return raw_sha256(raw)
        projected = copy.deepcopy(publisher)
        projected_observations = _objects(
            _object(projected["observed"])["published_outputs"]
        )
        projected_observations[index].clear()
        projected_observations[index].update(expected)
        if entry_present(destination):
            require_published_bytes(destination, raw)
        else:
            publish_immutable(destination, raw, publisher_claim_id)
        _ = validate_terminal_publisher_claim(projected, terminal_root)
        current.clear()
        current.update(expected)
        session.commit()
        _ = validate_terminal_publisher_claim(publisher, terminal_root)
    return raw_sha256(raw)


def _publisher(claims: list[JsonObject], claim_id: str) -> JsonObject:
    matches = [item for item in claims if item.get("claim_id") == claim_id]
    if len(matches) != 1:
        _fail("terminal publisher claim identity is missing or duplicated")
    return matches[0]


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail("terminal publisher value is not an object")
    return value


def _objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail("terminal publisher value is not an object array")
    return [item for item in value if isinstance(item, dict)]


def _fail(message: str) -> Never:
    raise IsolationError(message)
