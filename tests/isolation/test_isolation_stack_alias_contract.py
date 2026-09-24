from __future__ import annotations

from typing import TYPE_CHECKING

from ops.testing.isolation_stack_service_live import observe_live_service

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject


def test_compose_partial_automatic_aliases_are_normalized() -> None:
    project = "clinic_phase1a_alias"
    network = f"{project}_default"
    container_id = "1" * 64
    image_id = "sha256:" + ("2" * 64)
    desired: JsonObject = {
        "command": ["postgres"],
        "environment_contract": {
            "absent_keys": [],
            "literal": [],
            "secret_keys": [],
        },
        "extra_hosts": [],
        "filesystem_contract": None,
        "gid": 0,
        "image_contract": None,
        "image_id": image_id,
        "name": "db",
        "network_mode": "bridge",
        "network_refs": [{"aliases": ["db"], "network_name": network}],
        "published_ports": [],
        "start_policy": "running-before-activation",
        "uid": 0,
        "volume_mounts": [],
    }
    current: JsonObject = {
        "command": ["postgres"],
        "config_user": "",
        "id": container_id,
        "image_id": image_id,
        "labels": [
            {"name": "com.docker.compose.project", "value": project},
            {"name": "com.docker.compose.service", "value": "db"},
        ],
        "mounts": [],
        "network_attachments": [
            {
                "aliases": [f"{project}-db-1", "db"],
                "network_id": "3" * 64,
                "network_name": network,
            }
        ],
        "network_mode": network,
        "published_ports": [],
        "state": "running",
    }

    observed = observe_live_service(desired, current, strict=True)

    assert observed["network_attachments"] == [
        {
            "aliases": ["db"],
            "network_id": "3" * 64,
            "network_name": network,
        }
    ]
