from __future__ import annotations

import hashlib
from typing import cast

import pytest
import rfc8785
from ops.testing.isolation_common import IsolationError, JsonObject
from ops.testing.isolation_stale_execution import (
    StaleExecutionAdapters,
    execute_stale_action,
)

from isolation.isolation_stale_docker_fixtures import (
    FakeDocker,
    container,
    network,
    volume,
)

PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"
CURRENT_BOOT = "22222222-2222-4222-8222-222222222222"
CLAIM_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CONTAINER_ID = "c" * 64
NETWORK_ID = "d" * 64


def test_stale_container_removal_requires_the_exact_id_and_owner_labels() -> None:
    # Given: the exact task-owned container is still running.
    docker = FakeDocker()
    labels = {
        "com.docker.compose.project": "clinic_task",
        "com.docker.compose.service": "db",
    }
    docker.containers[CONTAINER_ID] = container(
        CONTAINER_ID,
        name="clinic_task-db-1",
        labels=labels,
    )
    identity = _identity(
        "container-remove-claim",
        "stop-remove-container",
        "container",
        container_id=CONTAINER_ID,
        expected_labels=[
            {"name": name, "value": value} for name, value in sorted(labels.items())
        ],
        expected_labels_exact=False,
    )

    # When: cleanup executes and then replays.
    _execute(identity, docker)
    _execute(identity, docker)

    # Then: stop precedes removal and no other selector is widened.
    assert CONTAINER_ID not in docker.containers
    assert docker.calls.index(
        ("container", "stop", "--time", "10", CONTAINER_ID)
    ) < docker.calls.index(("container", "rm", CONTAINER_ID))


def test_stale_container_refuses_a_recreated_owner_labeled_container() -> None:
    # Given: the old ID is absent but a replacement carries its ownership labels.
    docker = FakeDocker()
    recreated_id = "e" * 64
    labels = {
        "com.docker.compose.project": "clinic_task",
        "com.docker.compose.service": "db",
    }
    docker.containers[recreated_id] = container(
        recreated_id,
        name="clinic_task-db-1",
        labels=labels,
    )
    identity = _identity(
        "container-remove-claim",
        "stop-remove-container",
        "container",
        container_id=CONTAINER_ID,
        expected_labels=[
            {"name": name, "value": value} for name, value in sorted(labels.items())
        ],
        expected_labels_exact=False,
    )

    # When / Then: union disagreement blocks before any stop or remove command.
    with pytest.raises(IsolationError, match="selector"):
        _execute(identity, docker)
    assert not any(call[1] in {"stop", "rm"} for call in docker.calls)


def test_stale_runner_intent_removes_only_the_exact_name_label_union() -> None:
    # Given: an intent-only runner matches its deterministic name and four labels.
    docker = FakeDocker()
    labels = {
        "clinic.phase1a.attempt": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "clinic.phase1a.claim": CLAIM_ID,
        "clinic.phase1a.runner-create-intent": "f" * 64,
        "clinic.phase1a.service": "browser",
    }
    docker.containers[CONTAINER_ID] = container(
        CONTAINER_ID,
        name=f"clinic-phase1a-runner-{CLAIM_ID}",
        labels=labels,
        state="created",
    )
    identity = _identity(
        "container-remove-claim",
        "stop-remove-container",
        "container",
        container_id=None,
        container_name=f"clinic-phase1a-runner-{CLAIM_ID}",
        expected_labels=[
            {"name": name, "value": value} for name, value in sorted(labels.items())
        ],
        expected_labels_exact=True,
    )

    # When / Then: created state is removed without an unnecessary stop.
    _execute(identity, docker)
    assert CONTAINER_ID not in docker.containers
    assert not any(call[1] == "stop" for call in docker.calls)


def test_stale_borrowed_network_detaches_only_recorded_containers() -> None:
    # Given: the exact borrowed network still attaches one recorded task container.
    docker = FakeDocker()
    docker.networks[NETWORK_ID] = network(
        NETWORK_ID,
        name="borrowed",
        containers=[CONTAINER_ID],
    )
    identity = _identity(
        "borrowed-network-remove-claim",
        "detach-borrowed-network",
        "borrowed-network-attachment",
        container_ids=[CONTAINER_ID],
        observed_resource=_network_observation("borrowed"),
    )

    # When: the attachment action executes.
    _execute(identity, docker)

    # Then: the owner network survives and only the recorded attachment is gone.
    assert NETWORK_ID in docker.networks
    assert docker.networks[NETWORK_ID]["containers"] == []


def test_stale_owned_network_and_volume_require_exact_metadata() -> None:
    # Given: one exact owned network and volume plus a drifted replay candidate.
    docker = FakeDocker()
    docker.networks[NETWORK_ID] = network(NETWORK_ID, name="owned", containers=[])
    docker.volumes["data"] = volume("data")
    network_identity = _identity(
        "owned-network-remove-claim",
        "remove-owned-network",
        "owned-network",
        observed_resource=_network_observation("owned"),
    )
    volume_identity = _identity(
        "owned-volume-remove-claim",
        "remove-owned-volume",
        "owned-volume",
        observed_resource=_volume_observation(),
    )

    # When: exact resources are removed, then a foreign same-name volume appears.
    _execute(network_identity, docker)
    _execute(volume_identity, docker)
    docker.volumes["data"] = volume("data")
    labels = docker.volumes["data"]["labels"]
    assert isinstance(labels, dict)
    labels["clinic.phase1a.owner"] = "foreign"

    # Then: idempotent network replay succeeds but metadata drift blocks deletion.
    _execute(network_identity, docker)
    with pytest.raises(IsolationError, match="identity"):
        _execute(volume_identity, docker)
    assert "data" in docker.volumes


def _network_observation(name: str) -> JsonObject:
    return {
        "attachable": False,
        "driver": "bridge",
        "internal": True,
        "labels": [{"name": "clinic.phase1a.owner", "value": "task"}],
        "network_id": NETWORK_ID,
        "network_name": name,
    }


def _volume_observation() -> JsonObject:
    return {
        "created_at": "2026-07-16T21:00:00Z",
        "driver": "local",
        "labels": [{"name": "clinic.phase1a.owner", "value": "task"}],
        "mountpoint": "/var/lib/docker/volumes/data/_data",
        "options": [],
        "scope": "local",
        "volume_name": "data",
    }


def _identity(
    action_id: str,
    operation: str,
    resource_kind: str,
    **extra: object,
) -> JsonObject:
    result: JsonObject = {
        "action_id": action_id,
        "attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "claim_id": CLAIM_ID,
        "operation": operation,
        "origin_claim_sha256": "a" * 64,
        "resource_kind": resource_kind,
        "schema_version": 1,
    }
    result.update(cast("JsonObject", extra))
    return result


def _execute(identity: JsonObject, docker: FakeDocker) -> None:
    action = {
        "action_id": identity["action_id"],
        "claim_id": identity["claim_id"],
        "identity_sha256": hashlib.sha256(rfc8785.dumps(identity)).hexdigest(),
        "operation": identity["operation"],
        "resource_kind": identity["resource_kind"],
    }
    execute_stale_action(
        action,
        identity,
        previous_boot_id=PREVIOUS_BOOT,
        current_boot_id=CURRENT_BOOT,
        adapters=StaleExecutionAdapters(docker_runner=docker),
    )
