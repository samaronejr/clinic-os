from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing import isolation_host_inventory

if TYPE_CHECKING:
    import pytest
    from ops.testing.isolation_common import JsonObject


def test_host_inventory_combines_docker_and_listener_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: closed Docker metadata and an independently captured listener set.
    docker: JsonObject = {"containers": [], "networks": [], "volumes": []}
    listener = {
        "argv_sha256": "a" * 64,
        "container_id": None,
        "executable_realpath": "/usr/bin/python3",
        "host": "127.0.0.1",
        "owner_kind": "process",
        "pid": 123,
        "port": 8080,
        "process_start_ticks": 456,
        "socket_inode": 789,
        "transport": "tcp",
    }
    monkeypatch.setattr(
        isolation_host_inventory, "capture_docker_metadata", lambda: docker
    )
    monkeypatch.setattr(
        isolation_host_inventory,
        "capture_loopback_listeners",
        lambda proc_root, containers: [listener],
    )

    # When: the live host aggregator creates the normalized ledger observation.
    observed = isolation_host_inventory.capture_host_inventory()

    # Then: the exact four inventory arrays cross the trust boundary together.
    assert set(observed) == {"containers", "listeners", "networks", "volumes"}
    assert observed["listeners"] == [listener]
    assert Path("/proc").is_absolute()


def test_live_attestation_projects_only_closed_ledger_inventory_fields() -> None:
    # Given: a live capture carries extra command, mount, and network posture.
    container_id = "a" * 64
    network_id = "b" * 64
    raw: JsonObject = {
        "containers": [
            {
                "command": ["postgres"],
                "config_user": "0:0",
                "health": "healthy",
                "id": container_id,
                "image_id": "sha256:" + ("c" * 64),
                "labels": [],
                "mount_targets": [],
                "mounts": [],
                "network_attachments": [],
                "network_mode": "none",
                "published_ports": [],
                "restart_count": 0,
                "state": "running",
            }
        ],
        "listeners": [],
        "networks": [
            {
                "attachable": False,
                "driver": "bridge",
                "id": network_id,
                "internal": False,
                "labels": [],
                "name": "clinic_default",
            }
        ],
        "volumes": [],
    }

    # When: live metadata crosses into baseline/drift comparison.
    projected = isolation_host_inventory.project_host_inventory(raw)

    # Then: attestation-only fields do not widen the canonical ledger schema.
    containers = projected["containers"]
    networks = projected["networks"]
    assert isinstance(containers, list)
    assert isinstance(containers[0], dict)
    assert set(containers[0]) == {
        "config_sha256",
        "health",
        "id",
        "labels",
        "published_ports",
        "restart_count",
        "state",
    }
    assert networks == [{"id": network_id, "labels": [], "name": "clinic_default"}]
