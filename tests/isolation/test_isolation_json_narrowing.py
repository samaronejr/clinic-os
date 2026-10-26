from __future__ import annotations

import copy
import json
import traceback

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_docker_attestation import (
    MOUNT_TEMPLATE,
    NETWORK_TEMPLATE,
    enrich_docker_attestation,
)
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


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        ('{"synthetic-private-payload":', "invalid-json"),
        ('{"synthetic-private-payload": "synthetic-private-env"}', "object"),
        ("null", "null"),
        ('"synthetic-private-payload"', "string"),
        ("17", "number"),
        ("1.5", "number"),
        ("true", "boolean"),
        ('["synthetic-private-payload", 17]', "array-non-string"),
        ('["", null]', "array-non-string"),
        ("[]", "array-empty"),
        ('["synthetic-private-payload", ""]', "array-empty-string"),
    ],
)
@pytest.mark.parametrize("invalid_first", [False, True])
def test_docker_attestation_rejects_malformed_command_json(
    raw: str, category: str, invalid_first: bool
) -> None:
    # Given: two unclaimed containers, only one with rejected command metadata.
    valid_id, invalid_id = "a" * 64, "b" * 64
    identifiers = [invalid_id, valid_id] if invalid_first else [valid_id, invalid_id]
    inventory: JsonObject = {
        "containers": [
            {
                "id": identifier,
                "labels": [
                    {"name": "synthetic-private-name", "value": "private-label"}
                ],
            }
            for identifier in identifiers
        ],
        "networks": [],
    }
    before = copy.deepcopy(inventory)
    commands: list[tuple[str, ...]] = []

    def run(arguments: tuple[str, ...]) -> str:
        commands.append(arguments)
        assert arguments[:3] == ("container", "inspect", "--format")
        if arguments[3] == "{{json .Config.Cmd}}":
            return raw if arguments[4] == invalid_id else '["postgres"]'
        assert arguments[3] in {MOUNT_TEMPLATE, NETWORK_TEMPLATE}
        return ""

    # When / Then: malformed Docker output cannot leak a decoder or collection error.
    with pytest.raises(IsolationError, match="container command") as caught:
        _ = enrich_docker_attestation(inventory, run)
    (note,) = caught.value.__notes__
    assert dict(part.split("=", 1) for part in note.split()) == {
        "container_id": invalid_id,
        "field": "Config.Cmd",
        "category": category,
    }
    assert isinstance(caught.value.__cause__, json.JSONDecodeError) == (
        category == "invalid-json"
    )
    rendered = "".join(traceback.format_exception(caught.value))
    assert not any(
        value in rendered
        for value in (
            "synthetic-private-payload",
            "synthetic-private-env",
            "synthetic-private-name",
            "private-label",
        )
    )
    assert inventory == before
    assert commands[-1] == (
        "container",
        "inspect",
        "--format",
        "{{json .Config.Cmd}}",
        invalid_id,
    )


@pytest.mark.parametrize(
    "identifier", ["", "a" * 63, "a" * 65, "A" * 64, "a" * 64 + "\n", "private-name"]
)
def test_docker_attestation_rejects_unvalidated_container_identity(
    identifier: str,
) -> None:
    # Given: an identifier that is unsafe to publish as diagnostic attribution.
    inventory: JsonObject = {"containers": [{"id": identifier}], "networks": []}

    # When / Then: reject it before invoking Docker or echoing the supplied value.
    with pytest.raises(IsolationError, match="container ID") as caught:
        enrich_docker_attestation(inventory, _unexpected_command)
    assert not getattr(caught.value, "__notes__", [])
    if identifier:
        assert identifier not in str(caught.value)


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
