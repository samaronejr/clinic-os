from __future__ import annotations

from typing import cast

import pytest
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
)
from ops.testing.isolation_stale_actions import build_stale_action_plan
from ops.testing.isolation_stale_records import (
    RecoveryDiscovery,
    RecoveryPreparation,
    build_prepared_recovery,
    complete_stale_action,
)
from ops.testing.isolation_stale_runner_recovery import (
    begin_stale_runner_removal,
    complete_stale_runner_removal,
    validate_runner_recovery_prefixes,
)

from isolation.isolation_stale_recovery_fixtures import (
    CURRENT_BOOT,
    TIMESTAMP,
    boot_observation,
    stale_claim,
    stale_ledger,
)
from isolation_claim_fixtures import CLAIM_ID


def test_runner_tombstone_wraps_the_exact_outer_action_atomically() -> None:
    # Given: a prepared prior-boot runner action and copied creation authority.
    journal = _prepared_runner_journal()
    action_id = f"container-remove-{CLAIM_ID}"

    # When: removal intent is persisted, then physical absence is acknowledged.
    intent = begin_stale_runner_removal(journal, action_id)
    completed = complete_stale_runner_removal(intent, action_id)

    # Then: intent precedes removal and removed appears with outer completion.
    intent_creation = _creation(intent)
    completed_creation = _creation(completed)
    assert intent_creation["state"] == "remove-intent"
    assert intent_creation["remove_reason"] == "owner-lost"
    assert completed_creation["state"] == "removed"
    assert completed["completed_action_ids"] == [action_id]
    validate_runner_recovery_prefixes(intent)
    validate_runner_recovery_prefixes(completed)


def test_runner_action_cannot_complete_without_its_removed_tombstone() -> None:
    # Given: a prepared runner whose copied creation is not at removal intent.
    journal = _prepared_runner_journal()
    action_id = f"container-remove-{CLAIM_ID}"

    # When / Then: generic completion cannot bypass the runner write-ahead state.
    with pytest.raises(IsolationError, match="runner recovery"):
        complete_stale_action(journal, action_id)


def test_runner_recovery_rejects_removed_without_atomic_outer_completion() -> None:
    # Given: an impossible crash record with removed copied state but no action prefix.
    journal = _prepared_runner_journal()
    creation = _creation(journal)
    creation["state"] = "removed"
    creation["remove_reason"] = "owner-lost"
    creation["remove_intent_at_utc"] = "2026-07-16T22:00:00.000002Z"
    creation["removed_at_utc"] = "2026-07-16T22:00:00.000003Z"

    # When / Then: replay validation rejects the non-atomic prefix.
    with pytest.raises(IsolationError, match="atomic action completion"):
        validate_runner_recovery_prefixes(journal)


def _prepared_runner_journal() -> JsonObject:
    claim = stale_claim(CLAIM_ID, "stack", [], purpose="host-http")
    claim["status"] = "prepared"
    claim["desired"] = {"project": "clinic_runner"}
    claim["observed"] = {
        "borrowed_networks": [],
        "borrowed_volumes": [],
        "container_ids": ["c" * 64],
        "listeners": [],
        "owned_networks": [],
        "owned_volumes": [],
        "services": [],
    }
    claim["runner_creation"] = cast(
        "JsonValue",
        {
            "container_id": "c" * 64,
            "container_name": f"clinic-phase1a-runner-{CLAIM_ID}",
            "create_argv_sha256": "d" * 64,
            "creation_boot_id": "11111111-1111-4111-8111-111111111111",
            "desired_service_sha256": "e" * 64,
            "intent_at_utc": "2026-07-16T21:00:00.000001Z",
            "intent_id": "44444444-4444-4444-8444-444444444444",
            "intent_sha256": "f" * 64,
            "remove_intent_at_utc": None,
            "remove_reason": None,
            "removed_at_utc": None,
            "schema_version": 1,
            "service_name": "browser",
            "state": "prepared",
        },
    )
    ledger = stale_ledger([claim])
    plan = build_stale_action_plan(ledger)
    return build_prepared_recovery(
        ledger,
        canonical_bytes(ledger),
        plan,
        RecoveryPreparation(
            CURRENT_BOOT,
            boot_observation(),
            RecoveryDiscovery(),
            TIMESTAMP,
        ),
    )


def _creation(journal: JsonObject) -> JsonObject:
    recoveries = journal["runner_recoveries"]
    assert isinstance(recoveries, list)
    recovery = recoveries[0]
    assert isinstance(recovery, dict)
    creation = recovery["runner_creation"]
    assert isinstance(creation, dict)
    return creation
