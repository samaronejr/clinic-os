from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol, cast

import pytest
import rfc8785
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
)

from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    runner_stack_spec,
    snapshot,
    stack_observation,
    write_immutable_json,
    write_spec,
)


class RunnerState(Protocol):
    def record_runner_intent(self, ledger_path: Path, claim_id: str) -> str: ...

    def prepare_runner_claim(
        self,
        ledger_path: Path,
        claim_id: str,
        observed_path: Path,
    ) -> None: ...

    def begin_runner_discard(
        self,
        ledger_path: Path,
        claim_id: str,
        reason: str,
    ) -> None: ...

    def finish_runner_discard(self, ledger_path: Path, claim_id: str) -> None: ...


def _runner_state() -> RunnerState:
    return cast(
        "RunnerState",
        importlib.import_module("ops.testing.isolation_runner_state"),
    )


def _runner_observation(spec: JsonObject, state: str) -> JsonObject:
    observed = stack_observation(spec)
    services = observed["services"]
    assert isinstance(services, list)
    assert isinstance(services[0], dict)
    services[0]["state"] = state
    return observed


def test_runner_intent_is_durable_before_any_created_identity(tmp_path: Path) -> None:
    # Given: one reserved prepared-attestation stack with empty observations.
    ledger_path = snapshot(tmp_path)
    spec = runner_stack_spec(CLAIM_ID)
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))

    # When: runner creation records its deterministic authority intent.
    intent_sha = _runner_state().record_runner_intent(ledger_path, CLAIM_ID)

    # Then: the intent hash binds immutable core fields and no container identity.
    ledger, _ = load_json(ledger_path)
    claims = ledger["claims"]
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    creation = claims[0]["runner_creation"]
    assert isinstance(creation, dict)
    core: JsonObject = {
        key: creation[key]
        for key in (
            "schema_version",
            "intent_id",
            "service_name",
            "container_name",
            "desired_service_sha256",
            "create_argv_sha256",
        )
    }
    core["attempt_id"] = ledger["attempt_id"]
    core["claim_id"] = CLAIM_ID
    core["creation_boot_id"] = ledger["boot_id"]
    assert intent_sha == raw_sha256(rfc8785.dumps(core))
    assert creation["state"] == "intent"
    assert creation["container_id"] is None
    assert creation["remove_reason"] is None
    assert claims[0]["status"] == "reserved"


def test_runner_prepare_activate_discard_and_release_preserve_tombstone(
    tmp_path: Path,
) -> None:
    # Given: a runner intent followed by an immutable created-state observation.
    ledger_path = snapshot(tmp_path)
    spec = runner_stack_spec(CLAIM_ID)
    transitions = claim_transitions()
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, spec))
    runner = _runner_state()
    runner.record_runner_intent(ledger_path, CLAIM_ID)
    created = _runner_observation(spec, "created")
    created_path = write_immutable_json(tmp_path / "runner-created.json", created)

    # When: creation is prepared, activated, then journaled through removal.
    runner.prepare_runner_claim(ledger_path, CLAIM_ID, created_path)
    prepared, _ = load_json(ledger_path)
    prepared_claims = prepared["claims"]
    assert isinstance(prepared_claims, list)
    assert isinstance(prepared_claims[0], dict)
    assert prepared_claims[0]["status"] == "prepared"
    creation = prepared_claims[0]["runner_creation"]
    assert isinstance(creation, dict)
    assert creation["state"] == "prepared"
    assert creation["container_id"] == "e" * 64
    running = _runner_observation(spec, "running")
    running_path = write_immutable_json(tmp_path / "runner-running.json", running)
    transitions.activate_claim(ledger_path, CLAIM_ID, running_path)
    active, _ = load_json(ledger_path)
    active_claims = active["claims"]
    assert isinstance(active_claims, list)
    assert isinstance(active_claims[0], dict)
    active_observed = active_claims[0]["observed"]
    with pytest.raises(IsolationError, match="removed"):
        transitions.release_claim(ledger_path, CLAIM_ID)
    runner.begin_runner_discard(ledger_path, CLAIM_ID, "owner-cleanup")
    removing, _ = load_json(ledger_path)
    removing_claims = removing["claims"]
    assert isinstance(removing_claims, list)
    assert isinstance(removing_claims[0], dict)
    assert removing_claims[0]["status"] == "active"
    assert removing_claims[0]["observed"] == active_observed
    runner.finish_runner_discard(ledger_path, CLAIM_ID)

    # Then: removed cleanup authority releases only after its root is absent.
    removed, _ = load_json(ledger_path)
    removed_claims = removed["claims"]
    assert isinstance(removed_claims, list)
    assert isinstance(removed_claims[0], dict)
    removed_creation = removed_claims[0]["runner_creation"]
    assert isinstance(removed_creation, dict)
    assert removed_creation["state"] == "removed"
    assert removed_claims[0]["status"] == "active"
    assert removed_claims[0]["observed"] == active_observed
    claim_root = Path(str(removed["attempt_root"])) / "claims" / CLAIM_ID
    claim_root.rmdir()
    transitions.release_claim(ledger_path, CLAIM_ID)
    released, _ = load_json(ledger_path)
    assert released["claims"] == []


def test_runner_discard_rejects_a_noncanonical_reason_without_mutation(
    tmp_path: Path,
) -> None:
    # Given: one durable runner intent and its canonical ledger bytes.
    ledger_path = snapshot(tmp_path)
    spec = runner_stack_spec(CLAIM_ID)
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))
    runner = _runner_state()
    runner.record_runner_intent(ledger_path, CLAIM_ID)
    before = ledger_path.read_bytes()

    # When: cleanup receives a reason outside the closed same-boot grammar.
    with pytest.raises(IsolationError, match="reason"):
        runner.begin_runner_discard(ledger_path, CLAIM_ID, "changed-boot")

    # Then: no canonical runner state or timestamp changes.
    assert ledger_path.read_bytes() == before
