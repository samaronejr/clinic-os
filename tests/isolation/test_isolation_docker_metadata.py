from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, cast

import pytest
from ops.testing.isolation_docker_attestation import MOUNT_TEMPLATE, NETWORK_TEMPLATE
from ops.testing.isolation_inventory import normalize_inventory

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject


def test_docker_inventory_uses_only_closed_list_and_inspect_fields() -> None:
    # Given: a scripted Docker Engine metadata surface with one resource of each kind.
    container_id = "a" * 64
    image_id = "sha256:" + ("b" * 64)
    network_id = "c" * 64
    responses = {
        ("container", "ls", "--all", "--quiet", "--no-trunc"): container_id + "\n",
        ("container", "inspect", "--format", "{{.Id}}", container_id): container_id
        + "\n",
        ("container", "inspect", "--format", "{{.Image}}", container_id): image_id
        + "\n",
        (
            "container",
            "inspect",
            "--format",
            "{{.Config.User}}",
            container_id,
        ): "1000:1000\n",
        (
            "container",
            "inspect",
            "--format",
            "{{.HostConfig.NetworkMode}}",
            container_id,
        ): "none\n",
        (
            "container",
            "inspect",
            "--format",
            "{{range .Mounts}}{{println .Destination}}{{end}}",
            container_id,
        ): "/var/lib/postgresql/data\n",
        (
            "container",
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            container_id,
        ): '{"clinic":"test"}\n',
        (
            "container",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Ports}}",
            container_id,
        ): "{}\n",
        (
            "container",
            "inspect",
            "--format",
            "{{.State.Status}}",
            container_id,
        ): "exited\n",
        (
            "container",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container_id,
        ): "\n",
        ("container", "inspect", "--format", "{{.RestartCount}}", container_id): "0\n",
        ("volume", "ls", "--quiet"): "clinic-data\n",
        ("volume", "inspect", "--format", "{{.Name}}", "clinic-data"): "clinic-data\n",
        ("volume", "inspect", "--format", "{{.Driver}}", "clinic-data"): "local\n",
        ("volume", "inspect", "--format", "{{.Scope}}", "clinic-data"): "local\n",
        (
            "volume",
            "inspect",
            "--format",
            "{{.CreatedAt}}",
            "clinic-data",
        ): "2026-01-01T00:00:00Z\n",
        (
            "volume",
            "inspect",
            "--format",
            "{{.Mountpoint}}",
            "clinic-data",
        ): "/var/lib/docker/volumes/clinic-data/_data\n",
        ("volume", "inspect", "--format", "{{json .Labels}}", "clinic-data"): "null\n",
        ("volume", "inspect", "--format", "{{json .Options}}", "clinic-data"): "null\n",
        ("network", "ls", "--quiet", "--no-trunc"): network_id + "\n",
        ("network", "inspect", "--format", "{{.Id}}", network_id): network_id + "\n",
        ("network", "inspect", "--format", "{{.Name}}", network_id): "none\n",
        ("network", "inspect", "--format", "{{json .Labels}}", network_id): "null\n",
    }
    commands: list[tuple[str, ...]] = []

    def run(arguments: tuple[str, ...]) -> str:
        commands.append(arguments)
        return responses[arguments]

    # When: the Docker inventory reader crosses the external command boundary.
    try:
        module = importlib.import_module("ops.testing.isolation_docker_metadata")
    except ModuleNotFoundError:
        pytest.fail("Docker metadata reader is missing")
    raw = module.capture_docker_metadata(run)

    # Then: normalization succeeds and no prohibited Docker operation was requested.
    normalized = normalize_inventory({**raw, "listeners": []})
    containers = normalized["containers"]
    volumes = normalized["volumes"]
    networks = normalized["networks"]
    assert isinstance(containers, list)
    assert isinstance(containers[0], dict)
    assert isinstance(volumes, list)
    assert isinstance(volumes[0], dict)
    assert isinstance(networks, list)
    assert isinstance(networks[0], dict)
    assert containers[0]["id"] == container_id
    assert volumes[0]["volume_name"] == "clinic-data"
    assert networks[0]["id"] == network_id
    assert all(command[0] in {"container", "volume", "network"} for command in commands)
    assert not {"exec", "logs", "stats", "cp"}.intersection(
        part for command in commands for part in command
    )


def test_docker_attestation_adds_only_claim_resource_and_service_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one normalized-input Docker capture plus exact attestation-only fields.
    module = importlib.import_module("ops.testing.isolation_docker_metadata")
    container_id = "a" * 64
    network_id = "c" * 64
    base: JsonObject = {
        "containers": [
            {
                "config_user": "0:0",
                "health": "healthy",
                "id": container_id,
                "image_id": "sha256:" + ("b" * 64),
                "labels": [],
                "mount_targets": ["/var/lib/postgresql/data"],
                "network_mode": "clinic_default",
                "published_ports": [],
                "restart_count": 0,
                "state": "running",
            }
        ],
        "networks": [{"id": network_id, "labels": [], "name": "clinic_default"}],
        "volumes": [],
    }
    monkeypatch.setattr(module, "capture_docker_metadata", lambda _run: base)
    responses = {
        (
            "container",
            "inspect",
            "--format",
            "{{json .Config.Cmd}}",
            container_id,
        ): '["postgres"]\n',
        (
            "container",
            "inspect",
            "--format",
            MOUNT_TEMPLATE,
            container_id,
        ): "volume|clinic-data|/docker/clinic-data|/var/lib/postgresql/data|true\n",
        (
            "container",
            "inspect",
            "--format",
            NETWORK_TEMPLATE,
            container_id,
        ): f"clinic_default|{network_id}|clinic-db-1,db,\n",
        (
            "network",
            "inspect",
            "--format",
            "{{.Driver}}",
            network_id,
        ): "bridge\n",
        (
            "network",
            "inspect",
            "--format",
            "{{.Internal}}",
            network_id,
        ): "false\n",
        (
            "network",
            "inspect",
            "--format",
            "{{.Attachable}}",
            network_id,
        ): "false\n",
    }

    # When: the claim attestation reader enriches that read-only capture.
    raw = module.capture_docker_attestation_metadata(
        lambda arguments: responses[arguments],
    )

    # Then: it exposes exact command, mount, attachment, and network posture only.
    containers = cast("list[JsonObject]", raw["containers"])
    networks = cast("list[JsonObject]", raw["networks"])
    assert containers[0]["command"] == ["postgres"]
    assert containers[0]["mounts"] == [
        {
            "read_only": False,
            "source": "clinic-data",
            "target": "/var/lib/postgresql/data",
            "type": "volume",
        }
    ]
    assert containers[0]["network_attachments"] == [
        {
            "aliases": ["clinic-db-1", "db"],
            "network_id": network_id,
            "network_name": "clinic_default",
        }
    ]
    assert networks[0] == {
        "attachable": False,
        "driver": "bridge",
        "id": network_id,
        "internal": False,
        "labels": [],
        "name": "clinic_default",
    }
