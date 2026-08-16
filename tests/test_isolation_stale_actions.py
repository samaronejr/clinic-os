from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import TYPE_CHECKING, cast

import rfc8785
from ops.testing.isolation_stale_actions import build_stale_action_plan

from isolation_claim_fixtures import CLAIM_ID, SECOND_CLAIM_ID
from isolation_stale_recovery_fixtures import stale_claim, stale_ledger

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonValue


def test_stale_action_plan_is_reverse_topological_and_identity_bound() -> None:
    # Given: an active process borrower and its active filesystem owner.
    owner = stale_claim(CLAIM_ID, "filesystem", [], purpose="owner-staging")
    borrower = stale_claim(
        SECOND_CLAIM_ID,
        "process",
        [CLAIM_ID],
        purpose="borrower-process",
    )
    ledger = stale_ledger([owner, borrower])

    # When: the changed-boot physical plan is frozen.
    plan = build_stale_action_plan(ledger)

    # Then: borrower absence/staging precedes owner staging and every hash binds.
    assert [item["action_id"] for item in plan.actions] == [
        f"process-absent-{SECOND_CLAIM_ID}",
        f"staging-remove-{SECOND_CLAIM_ID}",
        f"staging-remove-{CLAIM_ID}",
    ]
    assert [
        hashlib.sha256(rfc8785.dumps(item)).hexdigest() for item in plan.identities
    ] == [item["identity_sha256"] for item in plan.actions]
    altered = deepcopy(plan.identities[0])
    altered["claim_id"] = CLAIM_ID
    assert (
        hashlib.sha256(rfc8785.dumps(altered)).hexdigest()
        != plan.actions[0]["identity_sha256"]
    )


def test_runner_plan_keeps_container_resources_and_staging_in_one_chain() -> None:
    # Given: a prepared runner with one borrowed and two owned stack resources.
    claim = stale_claim(CLAIM_ID, "stack", [], purpose="host-http")
    claim["status"] = "prepared"
    claim["runner_creation"] = cast(
        "JsonValue",
        {
            "container_id": "d" * 64,
            "container_name": "clinic-phase1a-runner",
            "create_argv_sha256": "e" * 64,
            "creation_boot_id": "11111111-1111-4111-8111-111111111111",
            "desired_service_sha256": "f" * 64,
            "intent_at_utc": "2026-07-16T21:00:00.000001Z",
            "intent_id": "44444444-4444-4444-8444-444444444444",
            "intent_sha256": "1" * 64,
            "remove_intent_at_utc": None,
            "remove_reason": None,
            "removed_at_utc": None,
            "schema_version": 1,
            "service_name": "browser",
            "state": "prepared",
        },
    )
    claim["observed"] = cast(
        "JsonValue",
        {
            "borrowed_networks": [
                {
                    "access": "attach",
                    "attachable": False,
                    "driver": "bridge",
                    "internal": False,
                    "labels": [],
                    "network_id": "2" * 64,
                    "network_name": "borrowed",
                    "owner_claim_id": SECOND_CLAIM_ID,
                }
            ],
            "borrowed_volumes": [],
            "container_ids": ["d" * 64],
            "listeners": [],
            "owned_networks": [
                {
                    "attachable": False,
                    "driver": "bridge",
                    "internal": True,
                    "labels": [],
                    "network_id": "3" * 64,
                    "network_name": "owned",
                }
            ],
            "owned_volumes": [
                {
                    "created_at": "2026-07-16T21:00:00Z",
                    "driver": "local",
                    "labels": [],
                    "mountpoint": "/var/lib/docker/volumes/data/_data",
                    "options": [],
                    "scope": "local",
                    "volume_name": "data",
                }
            ],
            "services": [],
        },
    )

    # When: changed-boot cleanup freezes the runner action chain.
    plan = build_stale_action_plan(stale_ledger([claim]))

    # Then: container removal does not suppress resource or staging cleanup.
    assert [item["resource_kind"] for item in plan.actions] == [
        "container",
        "borrowed-network-attachment",
        "owned-network",
        "owned-volume",
        "filesystem-staging",
    ]
    assert len(plan.runner_recoveries) == 1
    assert plan.runner_recoveries[0]["action_id"] == plan.actions[0]["action_id"]
