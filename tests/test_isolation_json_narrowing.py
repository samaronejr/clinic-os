from __future__ import annotations

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_docker_attestation import enrich_docker_attestation
from ops.testing.isolation_host_inventory import project_host_inventory
from ops.testing.isolation_stack_live import observe_live_stack


def _unexpected_command(_: tuple[str, ...]) -> str:
    message = "Docker command must not run for malformed inventory"
    raise AssertionError(message)


@pytest.mark.parametrize("containers", ["not-a-list", [None]])
def test_docker_attestation_rejects_malformed_container_arrays(
    containers: JsonValue,
) -> None:
    # Given: live Docker metadata with either the wrong collection shape or item type.
    inventory: JsonObject = {"containers": containers, "networks": []}

    # When / Then: the public attestation boundary fails closed with its typed error.
    with pytest.raises(IsolationError, match="containers"):
        _ = enrich_docker_attestation(inventory, _unexpected_command)


@pytest.mark.parametrize("raw", ["{", "{}"])
def test_docker_attestation_rejects_malformed_command_json(raw: str) -> None:
    # Given: one container whose command output is invalid JSON or not a list.
    inventory: JsonObject = {
        "containers": [{"id": "a" * 64}],
        "networks": [],
    }

    def run(_: tuple[str, ...]) -> str:
        return raw

    # When / Then: malformed Docker output cannot leak a decoder or collection error.
    with pytest.raises(IsolationError, match="container command"):
        _ = enrich_docker_attestation(inventory, run)


def test_host_projection_rejects_incomplete_container_objects() -> None:
    # Given: a dictionary item that is missing a required ledger-projection field.
    inventory: JsonObject = {
        "containers": [
            {
                "config_user": "",
                "health": "none",
                "id": "a" * 64,
                "image_id": "sha256:" + "b" * 64,
                "labels": [],
                "mount_targets": [],
                "network_mode": "bridge",
                "published_ports": [],
                "restart_count": 0,
            }
        ],
        "listeners": [],
        "networks": [],
        "volumes": [],
    }

    # When / Then: a missing live field is a typed boundary rejection, never KeyError.
    with pytest.raises(IsolationError, match="projection"):
        _ = project_host_inventory(inventory)


@pytest.mark.parametrize("services", ["not-a-list", [None]])
def test_live_stack_rejects_malformed_service_arrays(services: JsonValue) -> None:
    # Given: a stack reservation whose service collection is malformed.
    claim: JsonObject = {
        "desired": {
            "project": "clinic_phase1a_narrowing",
            "services": services,
        }
    }

    # When / Then: live observation rejects it with IsolationError before host matching.
    with pytest.raises(IsolationError, match="desired services"):
        _ = observe_live_stack(claim, {"containers": []})
