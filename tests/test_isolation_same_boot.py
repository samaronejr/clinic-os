from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_reconcile import reconcile_same_boot
from ops.testing.isolation_refresh import verify_claim

from isolation_claim_fixtures import (
    CLAIM_ID,
    OUTPUT_BYTES,
    SECOND_CLAIM_ID,
    claim_transitions,
    filesystem_spec,
    snapshot,
    write_immutable_json,
    write_spec,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _inventory_reader(ledger_path: Path) -> Callable[[], JsonObject]:
    ledger, _ = load_json(ledger_path)
    baseline = cast("JsonObject", ledger["baseline"])
    inventory: JsonObject = {
        key: copy.deepcopy(baseline[key])
        for key in ("containers", "listeners", "networks", "volumes")
    }
    return lambda: copy.deepcopy(inventory)


def _activate_filesystem(
    tmp_path: Path,
    ledger_path: Path,
    claim_id: str,
    dependencies: list[JsonValue],
) -> Path:
    transitions = claim_transitions()
    transitions.reserve_claim(
        ledger_path,
        write_spec(tmp_path, filesystem_spec(claim_id, dependencies)),
    )
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / claim_id
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
    observed_path = write_immutable_json(
        tmp_path / f"{claim_id}-observed.json",
        observed,
    )
    transitions.activate_claim(ledger_path, claim_id, observed_path)
    return owned_path


def test_verify_refreshes_the_complete_active_dependency_dag(tmp_path: Path) -> None:
    # Given: an active owner and dependent with unchanged staged identities.
    ledger_path = snapshot(tmp_path)
    _activate_filesystem(tmp_path, ledger_path, CLAIM_ID, [])
    _activate_filesystem(tmp_path, ledger_path, SECOND_CLAIM_ID, [CLAIM_ID])
    before, _ = load_json(ledger_path)
    before_claims = cast("list[JsonObject]", before["claims"])
    prior = {
        str(item["claim_id"]): str(item["last_verified_at_utc"])
        for item in before_claims
    }

    # When: the dependent receives one same-boot refresh.
    verify_claim(
        ledger_path,
        SECOND_CLAIM_ID,
        refresh=True,
        inventory_reader=_inventory_reader(ledger_path),
    )

    # Then: dependency-first live checks refresh both exact active claims.
    refreshed, _ = load_json(ledger_path)
    claims = cast("list[JsonObject]", refreshed["claims"])
    current = {
        str(item["claim_id"]): str(item["last_verified_at_utc"]) for item in claims
    }
    assert current[CLAIM_ID] > prior[CLAIM_ID]
    assert current[SECOND_CLAIM_ID] > prior[SECOND_CLAIM_ID]
    assert current[CLAIM_ID] <= current[SECOND_CLAIM_ID]


def test_verify_rejects_stale_or_drifted_authority_without_mutation(
    tmp_path: Path,
) -> None:
    # Given: one active claim whose refresh timestamp and file bytes are stale.
    ledger_path = snapshot(tmp_path)
    owned_path = _activate_filesystem(tmp_path, ledger_path, CLAIM_ID, [])
    ledger, _ = load_json(ledger_path)
    claims = cast("list[JsonObject]", ledger["claims"])
    claims[0]["last_verified_at_utc"] = "2000-01-01T00:00:00.000000Z"
    ledger["last_verified_at_utc"] = "2000-01-01T00:00:00.000000Z"
    write_atomic_replace(ledger_path, canonical_bytes(ledger))
    stale_bytes = ledger_path.read_bytes()

    # When / Then: a nonrefreshing consumer rejects expired authority unchanged.
    with pytest.raises(IsolationError, match="fresh"):
        verify_claim(
            ledger_path,
            CLAIM_ID,
            refresh=False,
            inventory_reader=_inventory_reader(ledger_path),
        )
    assert ledger_path.read_bytes() == stale_bytes

    # When / Then: refresh also refuses changed owned bytes without timestamping.
    owned_path.write_bytes(b"drift\n")
    owned_path.chmod(0o600)
    with pytest.raises(IsolationError, match="drift"):
        verify_claim(
            ledger_path,
            CLAIM_ID,
            refresh=True,
            inventory_reader=_inventory_reader(ledger_path),
        )
    assert ledger_path.read_bytes() == stale_bytes


def test_reconcile_activates_complete_and_prunes_empty_reservations(
    tmp_path: Path,
) -> None:
    # Given: one fully materialized reservation and one exact empty reservation.
    ledger_path = snapshot(tmp_path)
    transitions = claim_transitions()
    transitions.reserve_claim(
        ledger_path,
        write_spec(tmp_path, filesystem_spec(CLAIM_ID, [])),
    )
    ledger, _ = load_json(ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    complete_file = attempt_root / "claims" / CLAIM_ID / "staging" / "output.json"
    complete_file.parent.mkdir(mode=0o700)
    write_no_replace(complete_file, OUTPUT_BYTES, mode=0o600)
    transitions.reserve_claim(
        ledger_path,
        write_spec(tmp_path, filesystem_spec(SECOND_CLAIM_ID, [])),
    )

    # When: same-boot reconciliation observes both reservations under one lock.
    reconcile_same_boot(ledger_path, inventory_reader=_inventory_reader(ledger_path))

    # Then: complete identity activates; empty authority and root are removed.
    reconciled, _ = load_json(ledger_path)
    claims = cast("list[JsonObject]", reconciled["claims"])
    assert [(item["claim_id"], item["status"]) for item in claims] == [
        (CLAIM_ID, "active")
    ]
    assert not (attempt_root / "claims" / SECOND_CLAIM_ID).exists()


@pytest.mark.parametrize("state", ["partial", "foreign"])
def test_reconcile_rejects_partial_or_foreign_state_without_mutation(
    tmp_path: Path,
    state: str,
) -> None:
    # Given: a reservation plus either partial staging or an ambient resource.
    ledger_path = snapshot(tmp_path)
    claim_transitions().reserve_claim(
        ledger_path,
        write_spec(tmp_path, filesystem_spec(CLAIM_ID, [])),
    )
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    inventory_reader = _inventory_reader(ledger_path)
    if state == "partial":
        (claim_root / "staging").mkdir(mode=0o700)
    else:
        inventory = inventory_reader()
        inventory["networks"] = [
            {"id": "f" * 64, "labels": [], "name": "foreign-network"}
        ]

        def changed_inventory() -> JsonObject:
            return copy.deepcopy(inventory)

        inventory_reader = changed_inventory
    before = ledger_path.read_bytes()

    # When / Then: ambiguity fails before physical or canonical mutation.
    with pytest.raises(IsolationError, match=state):
        reconcile_same_boot(ledger_path, inventory_reader=inventory_reader)
    assert ledger_path.read_bytes() == before
    assert claim_root.exists()
