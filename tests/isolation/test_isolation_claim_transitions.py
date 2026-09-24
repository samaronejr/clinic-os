from __future__ import annotations

from pathlib import Path

import pytest
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
    write_no_replace,
)

from isolation_claim_fixtures import (
    CLAIM_ID,
    MISSING_CLAIM_ID,
    OUTPUT_BYTES,
    claim_transitions,
    filesystem_spec,
    snapshot,
    write_immutable_json,
    write_spec,
)


def test_filesystem_reservation_records_authority_before_creating_root(
    tmp_path: Path,
) -> None:
    # Given: an open snapshot and one closed immutable filesystem claim spec.
    ledger_path = snapshot(tmp_path)
    spec_path = write_spec(tmp_path, filesystem_spec(CLAIM_ID, []))

    # When: the stable-lock transition reserves that claim.
    reserved = claim_transitions().reserve_claim(ledger_path, spec_path)

    # Then: the exact reserved record and private root exist under the attempt.
    ledger, _ = load_json(ledger_path)
    assert reserved == CLAIM_ID
    assert ledger["claims"] == [
        {
            "activated_at_utc": None,
            "candidate_envelope_binding": None,
            "claim_id": CLAIM_ID,
            "dependency_claim_ids": [],
            "desired": filesystem_spec(CLAIM_ID, [])["desired"],
            "kind": "filesystem",
            "last_verified_at_utc": ledger["last_verified_at_utc"],
            "observed": {"owned_files": [], "published_outputs": []},
            "prepared_at_utc": None,
            "purpose": "todo-evidence-staging",
            "reserved_at_utc": ledger["last_verified_at_utc"],
            "root_relative_path": f"claims/{CLAIM_ID}",
            "runner_creation": None,
            "status": "reserved",
        }
    ]
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    assert claim_root.is_dir()
    assert claim_root.stat().st_mode & 0o777 == 0o700


def test_reservation_rejects_a_missing_dependency_without_mutation(
    tmp_path: Path,
) -> None:
    # Given: an open snapshot and a spec naming a nonexistent dependency.
    ledger_path = snapshot(tmp_path)
    before = ledger_path.read_bytes()
    spec = filesystem_spec(CLAIM_ID, [MISSING_CLAIM_ID])
    spec_path = write_spec(tmp_path, spec)

    # When: reservation attempts to bind the invalid dependency edge.
    with pytest.raises(IsolationError, match="dependency"):
        claim_transitions().reserve_claim(ledger_path, spec_path)

    # Then: neither the canonical ledger nor the claim root changes.
    ledger, _ = load_json(ledger_path)
    assert ledger_path.read_bytes() == before
    assert ledger["claims"] == []
    assert not (Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID).exists()


def test_filesystem_activation_records_fresh_nofollow_file_identity(
    tmp_path: Path,
) -> None:
    # Given: a reserved filesystem claim and its exact private staging file.
    ledger_path = snapshot(tmp_path)
    spec_path = write_spec(tmp_path, filesystem_spec(CLAIM_ID, []))
    transitions = claim_transitions()
    transitions.reserve_claim(ledger_path, spec_path)
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    staging = claim_root / "staging"
    staging.mkdir(mode=0o700)
    owned_path = staging / "output.json"
    write_no_replace(owned_path, OUTPUT_BYTES, mode=0o600)
    identity = owned_path.stat(follow_symlinks=False)
    observed: JsonObject = {
        "owned_files": [
            {
                "device": identity.st_dev,
                "gid": identity.st_gid,
                "inode": identity.st_ino,
                "mode": identity.st_mode & 0o777,
                "relative_path": "staging/output.json",
                "sha256": raw_sha256(OUTPUT_BYTES),
                "uid": identity.st_uid,
            }
        ],
        "published_outputs": [],
    }
    observed_path = write_immutable_json(tmp_path / "observed.json", observed)

    # When: activation re-observes the staging inode under the stable lock.
    transitions.activate_claim(ledger_path, CLAIM_ID, observed_path)

    # Then: the claim is active with the exact observed identity and timestamp.
    activated, _ = load_json(ledger_path)
    claims = activated["claims"]
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    assert claims[0]["status"] == "active"
    assert claims[0]["observed"] == observed
    assert claims[0]["activated_at_utc"] == activated["last_verified_at_utc"]


def test_filesystem_release_requires_the_mutable_root_absent(tmp_path: Path) -> None:
    # Given: one reserved claim whose private root still exists.
    ledger_path = snapshot(tmp_path)
    spec_path = write_spec(tmp_path, filesystem_spec(CLAIM_ID, []))
    transitions = claim_transitions()
    transitions.reserve_claim(ledger_path, spec_path)
    before = ledger_path.read_bytes()

    # When: release is attempted before owner cleanup, then after exact removal.
    with pytest.raises(IsolationError, match="claim root"):
        transitions.release_claim(ledger_path, CLAIM_ID)
    assert ledger_path.read_bytes() == before
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    claim_root.rmdir()
    transitions.release_claim(ledger_path, CLAIM_ID)

    # Then: only the absent claim is removed from the canonical ledger.
    released, _ = load_json(ledger_path)
    assert released["claims"] == []
