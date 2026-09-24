from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject, load_json

from isolation.isolation_runner_docker_fixtures import (
    FakeRunnerDocker,
    exact_runner_container,
)
from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    runner_stack_spec,
    snapshot,
    write_spec,
)

if TYPE_CHECKING:
    from pathlib import Path


class RunnerLifecycle(Protocol):
    def create_runner(
        self,
        ledger_path: Path,
        claim_id: str,
        docker_runner: FakeRunnerDocker | None = None,
    ) -> str: ...

    def discard_runner(
        self,
        ledger_path: Path,
        claim_id: str,
        reason: str,
        docker_runner: FakeRunnerDocker | None = None,
    ) -> None: ...


class SameBootCoordinator(Protocol):
    def reconcile_same_boot(
        self,
        ledger_path: Path,
        *,
        docker_runner: FakeRunnerDocker | None = None,
        inventory_reader: object | None = None,
    ) -> None: ...


def test_runner_create_fsyncs_intent_before_exact_docker_create(
    tmp_path: Path,
) -> None:
    # Given: one reserved runner and a Docker create probe.
    ledger_path = _reserved_runner(tmp_path)
    docker = FakeRunnerDocker()

    def create_container(arguments: tuple[str, ...]) -> JsonObject:
        intent = _claim(ledger_path)
        creation = cast("JsonObject", intent["runner_creation"])
        assert creation["state"] == "intent"
        assert arguments[:4] == ("create", "--rm", "--interactive", "--name")
        ledger, _ = load_json(ledger_path)
        return exact_runner_container(ledger)

    docker.on_create = create_container

    # When: the closed create operation owns the full write-ahead transition.
    identifier = _lifecycle().create_runner(ledger_path, CLAIM_ID, docker)

    # Then: the exact never-started container is durably prepared once.
    claim = _claim(ledger_path)
    creation = cast("JsonObject", claim["runner_creation"])
    assert identifier == "e" * 64
    assert claim["status"] == "prepared"
    assert creation["state"] == "prepared"
    assert creation["container_id"] == identifier


def test_runner_create_adopts_only_the_exact_never_started_intent(
    tmp_path: Path,
) -> None:
    # Given: Docker create completed after intent fsync but before prepare commit.
    ledger_path = _reserved_runner(tmp_path)
    state = importlib.import_module("ops.testing.isolation_runner_state")
    state.record_runner_intent(ledger_path, CLAIM_ID)
    ledger, _ = load_json(ledger_path)
    docker = FakeRunnerDocker()
    docker.containers["e" * 64] = exact_runner_container(ledger)

    # When: the same-boot owner replays runner-create.
    identifier = _lifecycle().create_runner(ledger_path, CLAIM_ID, docker)

    # Then: it prepares the exact candidate without issuing a second create.
    assert identifier == "e" * 64
    assert not any(call and call[0] == "create" for call in docker.calls)
    assert _claim(ledger_path)["status"] == "prepared"


def test_runner_create_refuses_foreign_name_label_disagreement(
    tmp_path: Path,
) -> None:
    # Given: a deterministic-name collision with a changed intent label.
    ledger_path = _reserved_runner(tmp_path)
    state = importlib.import_module("ops.testing.isolation_runner_state")
    state.record_runner_intent(ledger_path, CLAIM_ID)
    ledger, _ = load_json(ledger_path)
    docker = FakeRunnerDocker()
    foreign = exact_runner_container(ledger)
    labels = cast("dict[str, str]", foreign["labels"])
    labels["clinic.phase1a.runner-create-intent"] = "f" * 64
    docker.containers["e" * 64] = foreign
    before = ledger_path.read_bytes()

    # When / Then: union disagreement blocks without create, stop, or removal.
    with pytest.raises(IsolationError, match="selector"):
        _lifecycle().create_runner(ledger_path, CLAIM_ID, docker)
    assert ledger_path.read_bytes() == before
    assert "e" * 64 in docker.containers
    assert not _mutations(docker)


def test_runner_create_removes_a_matching_premature_start_then_fails(
    tmp_path: Path,
) -> None:
    # Given: the exact intended container already ran before attestation.
    ledger_path = _reserved_runner(tmp_path)
    state = importlib.import_module("ops.testing.isolation_runner_state")
    state.record_runner_intent(ledger_path, CLAIM_ID)
    ledger, _ = load_json(ledger_path)
    docker = FakeRunnerDocker()
    docker.containers["e" * 64] = exact_runner_container(ledger, state="running")

    # When / Then: unsafe-state is journaled, removed, released, and rejected.
    with pytest.raises(IsolationError, match="prematurely started"):
        _lifecycle().create_runner(ledger_path, CLAIM_ID, docker)
    assert docker.containers == {}
    released, _ = load_json(ledger_path)
    assert released["claims"] == []
    assert ("container", "stop", "--time", "10", "e" * 64) in docker.calls


def test_runner_discard_replays_remove_intent_before_physical_cleanup(
    tmp_path: Path,
) -> None:
    # Given: an exact created runner and a crash-persisted removal intent.
    ledger_path = _reserved_runner(tmp_path)
    docker = FakeRunnerDocker()
    docker.on_create = lambda _arguments: exact_runner_container(
        load_json(ledger_path)[0]
    )
    _lifecycle().create_runner(ledger_path, CLAIM_ID, docker)
    state = importlib.import_module("ops.testing.isolation_runner_state")
    state.begin_runner_discard(ledger_path, CLAIM_ID, "owner-cleanup")

    def assert_write_ahead() -> None:
        creation = cast("JsonObject", _claim(ledger_path)["runner_creation"])
        assert creation["state"] == "remove-intent"

    docker.before_mutation = assert_write_ahead

    # When: runner-discard resumes the exact same reason.
    _lifecycle().discard_runner(
        ledger_path,
        CLAIM_ID,
        "owner-cleanup",
        docker,
    )

    # Then: exact cleanup is complete, the empty root is gone, and claim released.
    released, _ = load_json(ledger_path)
    assert released["claims"] == []
    assert docker.containers == {}


def test_same_boot_reconcile_delegates_runner_intent_to_owner_lost_cleanup(
    tmp_path: Path,
) -> None:
    # Given: a crash-left intent and its exact never-started container.
    ledger_path = _reserved_runner(tmp_path)
    state = importlib.import_module("ops.testing.isolation_runner_state")
    state.record_runner_intent(ledger_path, CLAIM_ID)
    ledger, _ = load_json(ledger_path)
    docker = FakeRunnerDocker()
    docker.containers["e" * 64] = exact_runner_container(ledger)
    empty_inventory: JsonObject = {
        "containers": [],
        "listeners": [],
        "networks": [],
        "volumes": [],
    }

    # When: same-boot reconciliation owns a dead controller's runner prefix.
    _coordinator().reconcile_same_boot(
        ledger_path,
        docker_runner=docker,
        inventory_reader=lambda: empty_inventory,
    )

    # Then: owner-lost removal completes before ordinary claim reconciliation.
    released, _ = load_json(ledger_path)
    assert released["claims"] == []
    assert docker.containers == {}


def _reserved_runner(tmp_path: Path) -> Path:
    ledger_path = snapshot(tmp_path)
    spec = runner_stack_spec(CLAIM_ID)
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))
    return ledger_path


def _claim(ledger_path: Path) -> JsonObject:
    ledger, _ = load_json(ledger_path)
    claims = ledger["claims"]
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    return claims[0]


def _mutations(docker: FakeRunnerDocker) -> list[tuple[str, ...]]:
    return [call for call in docker.calls if call and call[0] in {"create", "rm"}]


def _lifecycle() -> RunnerLifecycle:
    return cast(
        "RunnerLifecycle",
        importlib.import_module("ops.testing.isolation_runner_lifecycle"),
    )


def _coordinator() -> SameBootCoordinator:
    return cast(
        "SameBootCoordinator",
        importlib.import_module("ops.testing.isolation_same_boot_coordinator"),
    )
