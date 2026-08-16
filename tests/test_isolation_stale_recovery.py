from __future__ import annotations

import pytest
from ops.testing.isolation_common import (
    IsolationError,
    canonical_bytes,
    raw_sha256,
)
from ops.testing.isolation_stale_actions import build_stale_action_plan
from ops.testing.isolation_stale_prune import (
    bind_claims_prune_intent,
    build_pruned_ledger,
    mark_claims_pruned,
)
from ops.testing.isolation_stale_records import (
    RecoveryDiscovery,
    RecoveryPreparation,
    build_prepared_recovery,
    complete_stale_action,
)

from isolation_claim_fixtures import CLAIM_ID, SECOND_CLAIM_ID
from isolation_stale_recovery_fixtures import (
    CURRENT_BOOT,
    JOURNAL_KEYS,
    PREVIOUS_BOOT,
    TIMESTAMP,
    boot_observation,
    stale_claim,
    stale_ledger,
)


def test_prepared_journal_is_closed_and_selects_resume_or_reject_immutably() -> None:
    # Given: one quiescent prior-boot ledger and its empty physical action plan.
    ledger = stale_ledger([])
    raw = canonical_bytes(ledger)
    action_plan = build_stale_action_plan(ledger)

    # When: discovery is quiescent, then separately proves a failure receipt.
    resume = build_prepared_recovery(
        ledger,
        raw,
        action_plan,
        RecoveryPreparation(
            CURRENT_BOOT,
            boot_observation(),
            RecoveryDiscovery(),
            TIMESTAMP,
        ),
    )
    reject = build_prepared_recovery(
        ledger,
        raw,
        action_plan,
        RecoveryPreparation(
            CURRENT_BOOT,
            boot_observation(),
            RecoveryDiscovery(has_failure_receipts=True),
            TIMESTAMP,
        ),
    )

    # Then: the closed prepared record binds every input and one fixed goal.
    assert set(resume) == JOURNAL_KEYS
    assert resume["state"] == "prepared"
    assert resume["recovery_goal"] == "resume"
    assert reject["recovery_goal"] == "reject"
    assert resume["prior_ledger_sha256"] == raw_sha256(raw)
    assert resume["completed_action_ids"] == []
    assert resume["post_cleanup_ledger_sha256"] is None
    assert resume["post_update_ledger_sha256"] is None


def test_prepared_journal_rejects_terminal_or_same_boot_entry() -> None:
    # Given: one prior ledger plus terminal-decision or same-boot discovery.
    ledger = stale_ledger([])
    raw = canonical_bytes(ledger)
    plan = build_stale_action_plan(ledger)

    # When / Then: neither state may be reclassified as nonsealed stale cleanup.
    with pytest.raises(IsolationError, match="terminal"):
        build_prepared_recovery(
            ledger,
            raw,
            plan,
            RecoveryPreparation(
                CURRENT_BOOT,
                boot_observation(),
                RecoveryDiscovery(terminal_decision=True),
                TIMESTAMP,
            ),
        )
    with pytest.raises(IsolationError, match="boot"):
        build_prepared_recovery(
            ledger,
            raw,
            plan,
            RecoveryPreparation(
                PREVIOUS_BOOT,
                boot_observation(),
                RecoveryDiscovery(),
                TIMESTAMP,
            ),
        )


def test_completed_actions_are_an_ordered_prefix() -> None:
    # Given: a prepared journal with three reverse-topological actions.
    ledger = stale_ledger(
        [
            stale_claim(CLAIM_ID, "filesystem", [], purpose="owner-staging"),
            stale_claim(
                SECOND_CLAIM_ID,
                "process",
                [CLAIM_ID],
                purpose="borrower-process",
            ),
        ]
    )
    plan = build_stale_action_plan(ledger)
    journal = build_prepared_recovery(
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

    # When / Then: skipping an action fails; exact completion reaches absence.
    with pytest.raises(IsolationError, match="next"):
        complete_stale_action(journal, f"staging-remove-{SECOND_CLAIM_ID}")
    current = journal
    for action in plan.actions:
        current = complete_stale_action(current, str(action["action_id"]))
    assert current["completed_action_ids"] == [
        item["action_id"] for item in plan.actions
    ]
    assert current["state"] == "resources-absent"


def test_claim_prune_is_write_ahead_and_preserves_the_sole_publisher() -> None:
    # Given: resources-absent recovery with one ordinary and one publisher claim.
    ordinary = stale_claim(CLAIM_ID, "filesystem", [], purpose="ordinary-staging")
    publisher = stale_claim(
        SECOND_CLAIM_ID,
        "filesystem",
        [],
        purpose="final-terminal-publisher",
    )
    ledger = stale_ledger([ordinary, publisher])
    raw = canonical_bytes(ledger)
    plan = build_stale_action_plan(ledger)
    journal = build_prepared_recovery(
        ledger,
        raw,
        plan,
        RecoveryPreparation(
            CURRENT_BOOT,
            boot_observation(),
            RecoveryDiscovery(),
            TIMESTAMP,
        ),
    )
    for action in plan.actions:
        journal = complete_stale_action(journal, str(action["action_id"]))

    # When: prospective prune bytes are bound, applied, and acknowledged.
    pruned, pruned_raw = build_pruned_ledger(ledger, journal)
    intent = bind_claims_prune_intent(journal, pruned_raw)
    completed = mark_claims_pruned(intent, pruned_raw)

    # Then: the prior object is untouched and the publisher bytes are preserved.
    assert canonical_bytes(ledger) == raw
    assert pruned["claims"] == [publisher]
    assert intent["post_cleanup_ledger_sha256"] == raw_sha256(pruned_raw)
    assert intent["state"] == "claims-prune-intent"
    assert completed["state"] == "claims-pruned"
