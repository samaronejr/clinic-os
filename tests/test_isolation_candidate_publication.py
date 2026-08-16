from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest
import rfc8785
from ops.testing.isolation_candidate_publication import (
    publish_candidate_envelope,
)
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
)

from isolation_candidate_fixtures import (
    accepting_verifier,
    candidate_envelope,
    reserve_and_activate_candidate,
    write_staged_envelope,
)
from isolation_claim_fixtures import CLAIM_ID, claim_transitions, snapshot


def test_candidate_binding_is_durable_before_envelope_publication(
    tmp_path: Path,
) -> None:
    # Given: an active candidate publisher and its exact immutable staged envelope.
    ledger_path = snapshot(tmp_path)
    spec = reserve_and_activate_candidate(tmp_path, ledger_path)
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    envelope = candidate_envelope(ledger)
    staged = write_staged_envelope(claim_root, envelope)

    # When: publication is interrupted immediately after binding is committed.
    with pytest.raises(RuntimeError, match="publication interrupted"):
        publish_candidate_envelope(
            ledger_path,
            CLAIM_ID,
            staged,
            verifier=accepting_verifier(),
            publisher=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("publication interrupted")
            ),
        )

    # Then: the ledger has the sole immutable binding and no destination bytes.
    bound, _ = load_json(ledger_path)
    claim = _claim(bound)
    binding = claim["candidate_envelope_binding"]
    assert isinstance(binding, dict)
    assert binding == {
        "envelope": envelope,
        "envelope_sha256": hashlib.sha256(rfc8785.dumps(envelope) + b"\n").hexdigest(),
        "schema_version": 1,
    }
    outputs = _observations(claim)
    assert outputs[0]["status"] == "unpublished"
    destination = _destination(_authorizations(spec)[0])
    assert not destination.exists()


def test_candidate_publish_replays_from_binding_without_staged_bytes(
    tmp_path: Path,
) -> None:
    # Given: a crash left a durable binding but no envelope destination.
    ledger_path = snapshot(tmp_path)
    spec = reserve_and_activate_candidate(tmp_path, ledger_path)
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    envelope = candidate_envelope(ledger)
    staged = write_staged_envelope(claim_root, envelope)
    with pytest.raises(RuntimeError):
        publish_candidate_envelope(
            ledger_path,
            CLAIM_ID,
            staged,
            verifier=accepting_verifier(),
            publisher=lambda *_args: (_ for _ in ()).throw(RuntimeError("crash")),
        )
    staged.unlink()

    # When: same-boot replay resumes with the exact staged path now absent.
    replayed = publish_candidate_envelope(
        ledger_path,
        CLAIM_ID,
        staged,
        verifier=lambda *_args: pytest.fail("bound replay reverified mutable inputs"),
    )

    # Then: bytes derive only from the binding and authorization becomes published.
    destination = _destination(_authorizations(spec)[0])
    assert replayed == canonical_bytes(envelope)
    assert destination.read_bytes() == replayed
    assert destination.stat().st_mode & 0o777 == 0o400
    published, _ = load_json(ledger_path)
    output = _observations(_claim(published))[0]
    assert output["status"] == "published"
    entries = _object_array(output["entries"])
    assert entries[0]["sha256"] == hashlib.sha256(replayed).hexdigest()


def test_candidate_release_publishes_history_after_envelope(tmp_path: Path) -> None:
    # Given: the envelope is binding-derived and durably authorized as published.
    ledger_path = snapshot(tmp_path)
    spec = reserve_and_activate_candidate(tmp_path, ledger_path)
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    staged = write_staged_envelope(claim_root, candidate_envelope(ledger))
    publish_candidate_envelope(
        ledger_path,
        CLAIM_ID,
        staged,
        verifier=accepting_verifier(),
    )
    published, _ = load_json(ledger_path)
    published_claim = _claim(published)
    binding = _object(published_claim["candidate_envelope_binding"])
    predecessor = _observations(published_claim)[0]
    predecessor_entries = predecessor["entries"]
    staged.unlink()
    claim_root.rmdir()

    # When: candidate-specific release completes the successor authorization.
    claim_transitions().release_claim(ledger_path, CLAIM_ID)

    # Then: the history tombstone survives and the publisher claim is absent.
    released, _ = load_json(ledger_path)
    assert released["claims"] == []
    authorizations = _authorizations(spec)
    history_auth = authorizations[1]
    history = _destination(history_auth)
    record, _ = load_json(history)
    assert record == {
        "attempt_id": published["attempt_id"],
        "authorization_id": "candidate-application-publication-history",
        "candidate_envelope_binding_sha256": hashlib.sha256(
            rfc8785.dumps(binding)
        ).hexdigest(),
        "claim_id": CLAIM_ID,
        "output_kind": "publication-history",
        "predecessor_authorization_id": "candidate-application-envelope",
        "predecessor_entries_sha256": hashlib.sha256(
            rfc8785.dumps(predecessor_entries)
        ).hexdigest(),
        "predecessor_root_path": authorizations[0]["root_path"],
        "purpose": "candidate-application-publisher",
        "relative_path": f"{CLAIM_ID}.json",
        "root_path": history_auth["root_path"],
        "schema_version": 1,
    }
    assert history.stat().st_mode & 0o777 == 0o400


def test_unbound_candidate_destination_rejects_without_ledger_mutation(
    tmp_path: Path,
) -> None:
    # Given: foreign bytes already occupy the envelope destination before binding.
    ledger_path = snapshot(tmp_path)
    spec = reserve_and_activate_candidate(tmp_path, ledger_path)
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    staged = write_staged_envelope(claim_root, candidate_envelope(ledger))
    authorization = _authorizations(spec)[0]
    destination = _destination(authorization)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"foreign\n")
    destination.chmod(0o400)
    before = ledger_path.read_bytes()

    # When: candidate publication detects an unbound destination.
    with pytest.raises(IsolationError, match="binding-null destination"):
        publish_candidate_envelope(
            ledger_path,
            CLAIM_ID,
            staged,
            verifier=accepting_verifier(),
        )

    # Then: no binding or authorization state was created.
    assert ledger_path.read_bytes() == before


def _claim(ledger: JsonObject) -> JsonObject:
    claims = _object_array(ledger["claims"])
    assert len(claims) == 1
    return claims[0]


def _authorizations(spec: JsonObject) -> list[JsonObject]:
    desired = _object(spec["desired"])
    return _object_array(desired["published_outputs"])


def _observations(claim: JsonObject) -> list[JsonObject]:
    observed = _object(claim["observed"])
    return _object_array(observed["published_outputs"])


def _destination(authorization: JsonObject) -> Path:
    root = authorization["root_path"]
    paths = authorization["relative_paths"]
    assert isinstance(root, str)
    assert isinstance(paths, list)
    assert len(paths) == 1
    assert isinstance(paths[0], str)
    return Path(root) / paths[0]


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _object_array(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast("list[JsonObject]", value)
