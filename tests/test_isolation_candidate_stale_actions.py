from __future__ import annotations

import hashlib
import os
from copy import deepcopy

import pytest
import rfc8785
from ops.testing.isolation_candidate_stale_actions import (
    build_candidate_stale_actions,
)

from isolation_candidate_stale_fixtures import (
    CLAIM_ID,
    EXPECTED_APPLICATION_HASHES,
    EXPECTED_RUNNER_HASHES,
    fixed_candidate_state,
)


@pytest.fixture(autouse=True)
def _pin_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getegid", lambda: 1000)


@pytest.mark.parametrize(
    ("runner", "expected_hashes"),
    [(False, EXPECTED_APPLICATION_HASHES), (True, EXPECTED_RUNNER_HASHES)],
)
def test_candidate_stale_actions_are_adjacent_and_identity_bound(
    runner: bool,
    expected_hashes: tuple[str, str, str],
) -> None:
    # Given: an active bound candidate whose canonical history is unpublished.
    ledger, claim = fixed_candidate_state(runner=runner)

    # When: changed-boot preparation derives its physical resource actions.
    actions, identities = build_candidate_stale_actions(ledger, claim)

    # Then: the unconditional envelope, staging, history chain has fixed hashes.
    assert [item["action_id"] for item in actions] == [
        f"candidate-envelope-complete-{CLAIM_ID}",
        f"candidate-staging-complete-{CLAIM_ID}",
        f"candidate-history-complete-{CLAIM_ID}",
    ]
    assert tuple(item["identity_sha256"] for item in actions) == expected_hashes
    assert [
        hashlib.sha256(rfc8785.dumps(item)).hexdigest() for item in identities
    ] == list(expected_hashes)
    assert identities[1]["predecessor_action_id"] == actions[0]["action_id"]
    assert identities[2]["predecessor_action_id"] == actions[1]["action_id"]
    assert identities[2]["envelope_action_id"] == actions[0]["action_id"]
    staged_entry = identities[1]["expected_staged_entry"]
    assert isinstance(staged_entry, dict)
    assert staged_entry["relative_path"] == "candidate-envelope.json"


def test_candidate_stale_identity_changes_on_any_bound_field_drift() -> None:
    # Given: the fixed application candidate action identities.
    ledger, claim = fixed_candidate_state(runner=False)
    _, identities = build_candidate_stale_actions(ledger, claim)
    original = hashlib.sha256(rfc8785.dumps(identities[2])).hexdigest()

    # When: one history root field is substituted.
    altered = deepcopy(identities[2])
    altered["root_path"] = "/foreign/history"

    # Then: the authenticated action identity no longer matches.
    assert hashlib.sha256(rfc8785.dumps(altered)).hexdigest() != original
