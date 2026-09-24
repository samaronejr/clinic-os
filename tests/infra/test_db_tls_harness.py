from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing.isolation_borrowed_resources import (
    observe_borrowed_resources,
    validate_borrowed_resources,
)
from ops.testing.isolation_stack_claim import validate_stack_desired
from ops.testing.process_helpers import run_process
from ops.testing.tls_contract import HBA_RULES, POSTGRES_IMAGE, VOLUME_SUFFIXES
from ops.testing.tls_specs import materializer_spec

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_stack_desired_accepts_dependency_owned_read_only_resources() -> None:
    owner_claim_id = "11111111-1111-4111-8111-111111111111"
    desired: JsonObject = {
        "borrowed_network_refs": [
            {
                "access": "attach",
                "network_name": "source_network",
                "owner_claim_id": owner_claim_id,
            }
        ],
        "borrowed_volume_refs": [
            {
                "access": "read-only",
                "owner_claim_id": owner_claim_id,
                "volume_name": "source_tls",
            }
        ],
        "database_names": [],
        "loopback_ports": [],
        "owned_networks": [],
        "owned_volumes": [],
        "project": "clinic_phase1a_consumer",
        "services": [
            {
                "command": ["postgres"],
                "environment_contract": {
                    "absent_keys": [],
                    "literal": [],
                    "secret_keys": [],
                },
                "extra_hosts": [],
                "filesystem_contract": None,
                "gid": 999,
                "image_contract": None,
                "image_id": f"sha256:{'2' * 64}",
                "name": "database",
                "network_mode": "bridge",
                "network_refs": [
                    {
                        "aliases": ["phase1a-db.qa.clinic-os.dev"],
                        "network_name": "source_network",
                    }
                ],
                "published_ports": [],
                "start_policy": "running-before-activation",
                "uid": 999,
                "volume_mounts": [
                    {
                        "read_only": True,
                        "target": "/run/clinic-test-db-tls",
                        "volume_name": "source_tls",
                    }
                ],
            }
        ],
    }

    validate_stack_desired(desired)


def test_borrowed_resource_observation_binds_owner_and_live_identity() -> None:
    owner_claim_id = "11111111-1111-4111-8111-111111111111"
    desired: JsonObject = {
        "borrowed_network_refs": [
            {
                "access": "attach",
                "network_name": "source_network",
                "owner_claim_id": owner_claim_id,
            }
        ],
        "borrowed_volume_refs": [
            {
                "access": "read-only",
                "owner_claim_id": owner_claim_id,
                "volume_name": "source_tls",
            }
        ],
    }
    observed: JsonObject = {
        "borrowed_networks": [
            {
                "access": "attach",
                "attachable": False,
                "driver": "bridge",
                "internal": False,
                "labels": [],
                "network_id": "2" * 64,
                "network_name": "source_network",
                "owner_claim_id": owner_claim_id,
            }
        ],
        "borrowed_volumes": [
            {
                "access": "read-only",
                "created_at": "2026-08-17T00:00:00Z",
                "driver": "local",
                "labels": [],
                "mountpoint": "/var/lib/docker/volumes/source_tls/_data",
                "options": [],
                "owner_claim_id": owner_claim_id,
                "scope": "local",
                "volume_name": "source_tls",
            }
        ],
    }

    validate_borrowed_resources(desired, observed)


def test_borrowed_resource_observer_records_exact_live_docker_identity() -> None:
    owner_claim_id = "11111111-1111-4111-8111-111111111111"
    desired: JsonObject = {
        "borrowed_network_refs": [
            {
                "access": "attach",
                "network_name": "source_network",
                "owner_claim_id": owner_claim_id,
            }
        ],
        "borrowed_volume_refs": [
            {
                "access": "read-only",
                "owner_claim_id": owner_claim_id,
                "volume_name": "source_tls",
            }
        ],
    }
    inventory: JsonObject = {
        "networks": [
            {
                "attachable": False,
                "driver": "bridge",
                "id": "2" * 64,
                "internal": False,
                "labels": [],
                "name": "source_network",
            }
        ],
        "volumes": [
            {
                "created_at": "2026-08-17T00:00:00Z",
                "driver": "local",
                "labels": [],
                "mountpoint": "/var/lib/docker/volumes/source_tls/_data",
                "options": [],
                "scope": "local",
                "volume_name": "source_tls",
            }
        ],
    }

    observed_volumes, observed_networks = observe_borrowed_resources(
        desired,
        inventory,
    )

    assert observed_volumes[0]["owner_claim_id"] == owner_claim_id
    assert observed_volumes[0]["access"] == "read-only"
    assert observed_networks[0]["network_id"] == "2" * 64
    assert observed_networks[0]["access"] == "attach"


def test_tls_materializer_contract_is_pinned_and_role_separated() -> None:
    assert POSTGRES_IMAGE == (
        "docker.io/library/postgres@"
        "sha256:4c9405bdf36a7a96c5637acec4b39545681f0d2154a7b1e622890607aad6bf56"
    )
    assert VOLUME_SUFFIXES == (
        "phase1a-pki-private",
        "phase1a-source-db-tls",
        "phase1a-restore-db-tls",
        "phase1a-web-tls",
        "phase1a-db-trust",
        "phase1a-materializer-pgdata",
    )
    assert HBA_RULES == (
        "local all postgres peer",
        "local all all reject",
        "hostnossl all all 0.0.0.0/0 reject",
        "hostnossl all all ::/0 reject",
        "hostssl all all 0.0.0.0/0 scram-sha-256",
        "hostssl all all ::/0 scram-sha-256",
    )


def test_tls_stack_cli_rejects_noncanonical_forms() -> None:
    script = PROJECT_ROOT / "ops/testing/tls_stack.sh"
    for arguments in (
        (),
        ("unknown",),
        ("smoke", "extra"),
        (
            "export-public-ca",
            "--claim",
            "11111111-1111-4111-8111-111111111111",
        ),
    ):
        result = run_process((str(script), *arguments))
        assert result.returncode == 2
        assert "tls-stack: invalid invocation" in result.stderr


def test_materializer_spec_owns_six_named_mounts_and_no_network() -> None:
    claim_id = "11111111-1111-4111-8111-111111111111"
    spec = materializer_spec(
        claim_id,
        "clinic_phase1a_tls",
        f"sha256:{'2' * 64}",
    )
    desired = spec["desired"]
    assert isinstance(desired, dict)
    volumes = desired["owned_volumes"]
    services = desired["services"]
    assert isinstance(volumes, list)
    assert isinstance(services, list)
    assert len(volumes) == 6
    assert len(services) == 1
    service = services[0]
    assert isinstance(service, dict)
    assert service["network_mode"] == "none"
    assert service["network_refs"] == []
    assert service["published_ports"] == []
    volume_mounts = service["volume_mounts"]
    assert isinstance(volume_mounts, list)
    assert len(volume_mounts) == 6
    assert desired["owned_networks"] == []
    assert desired["loopback_ports"] == []
    assert desired["database_names"] == []
