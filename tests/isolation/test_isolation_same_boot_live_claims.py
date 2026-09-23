from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from ops.testing import isolation_reconcile, isolation_refresh
from ops.testing.isolation_candidate_publication import publish_candidate_envelope
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    load_json,
)
from ops.testing.isolation_host_state import require_known_host_state
from ops.testing.isolation_reconcile import reconcile_same_boot
from ops.testing.isolation_refresh import verify_claim
from ops.testing.isolation_stack_live import observe_live_stack

from isolation.isolation_candidate_fixtures import (
    accepting_verifier,
    candidate_envelope,
    reserve_and_activate_candidate,
    write_staged_envelope,
)
from isolation.isolation_process_fixtures import process_spec
from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    snapshot,
    stack_observation,
    stack_spec,
    write_immutable_json,
    write_spec,
)

if TYPE_CHECKING:
    from collections.abc import Callable

type ClaimObserver = Callable[[JsonObject, JsonObject], JsonObject | None]


def test_reconcile_adopts_a_complete_reserved_stack_from_live_identity(
    tmp_path: Path,
) -> None:
    # Given: a reserved stack whose exact service and listener now exist.
    ledger_path = snapshot(tmp_path)
    spec = stack_spec(CLAIM_ID, "clinic_adopt", 18081)
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))
    observed = stack_observation(spec)
    inventory = _inventory_with_stack(ledger_path, observed, "clinic_adopt")

    # When: same-boot reconciliation receives a fresh closed live observation.
    reconcile_same_boot(
        ledger_path,
        inventory_reader=lambda: copy.deepcopy(inventory),
        claim_observer=_observer(observed),
    )

    # Then: the reservation becomes active with the exact observed identity.
    ledger, _ = load_json(ledger_path)
    claims = cast("list[JsonObject]", ledger["claims"])
    assert claims[0]["status"] == "active"
    assert claims[0]["observed"] == observed


def test_reconcile_separates_live_attestation_from_the_ledger_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a reserved stack and one raw live capture with private config fields.
    ledger_path = snapshot(tmp_path)
    spec = stack_spec(CLAIM_ID, "clinic_live_projection", 18082)
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))
    observed = stack_observation(spec)
    raw_inventory = _inventory_with_stack(
        ledger_path,
        observed,
        "clinic_live_projection",
    )
    monkeypatch.setattr(
        isolation_reconcile,
        "capture_host_inventory",
        lambda: copy.deepcopy(raw_inventory),
    )
    monkeypatch.setattr(
        isolation_reconcile,
        "capture_live_host_inventory",
        lambda: copy.deepcopy(raw_inventory),
    )
    host_state_validator = require_known_host_state

    def require_closed_projection(ledger: JsonObject, current: JsonObject) -> None:
        containers = cast("list[JsonObject]", current["containers"])
        assert set(containers[0]) == {
            "config_sha256",
            "health",
            "id",
            "labels",
            "published_ports",
            "restart_count",
            "state",
        }
        host_state_validator(ledger, current)

    monkeypatch.setattr(
        isolation_reconcile,
        "require_known_host_state",
        require_closed_projection,
    )

    # When: production same-boot reconciliation captures and adopts the stack.
    isolation_reconcile.reconcile_same_boot(ledger_path)

    # Then: the closed ledger projection remains separate from live config metadata.
    ledger, _ = load_json(ledger_path)
    claims = cast("list[JsonObject]", ledger["claims"])
    assert claims[0]["status"] == "active"
    assert claims[0]["observed"] == observed


def test_refresh_separates_live_attestation_from_the_ledger_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an active stack and one raw live capture with private config fields.
    ledger_path = snapshot(tmp_path)
    spec = stack_spec(CLAIM_ID, "clinic_refresh_projection", 18083)
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))
    observed = stack_observation(spec)
    raw_inventory = _inventory_with_stack(
        ledger_path,
        observed,
        "clinic_refresh_projection",
    )
    reconcile_same_boot(
        ledger_path,
        inventory_reader=lambda: copy.deepcopy(raw_inventory),
        claim_observer=_observer(observed),
    )
    monkeypatch.setattr(
        isolation_refresh,
        "capture_host_inventory",
        lambda: copy.deepcopy(raw_inventory),
    )
    monkeypatch.setattr(
        isolation_refresh,
        "capture_live_host_inventory",
        lambda: copy.deepcopy(raw_inventory),
    )
    host_state_validator = require_known_host_state

    def require_closed_projection(ledger: JsonObject, current: JsonObject) -> None:
        containers = cast("list[JsonObject]", current["containers"])
        assert set(containers[0]) == {
            "config_sha256",
            "health",
            "id",
            "labels",
            "published_ports",
            "restart_count",
            "state",
        }
        host_state_validator(ledger, current)

    monkeypatch.setattr(
        isolation_refresh,
        "require_known_host_state",
        require_closed_projection,
    )

    # When: production refresh re-attests the active stack.
    isolation_refresh.verify_claim(ledger_path, CLAIM_ID, refresh=True)

    # Then: the exact active observation remains unchanged.
    ledger, _ = load_json(ledger_path)
    claims = cast("list[JsonObject]", ledger["claims"])
    assert claims[0]["observed"] == observed


def test_live_stack_observer_attests_owned_docker_resources_and_mappings(
    tmp_path: Path,
) -> None:
    # Given: a reserved resourceful stack and its field-specific Docker capture.
    ledger_path = snapshot(tmp_path)
    project = "clinic_resourceful_live"
    network_name = f"{project}_default"
    volume_name = f"{project}_data"
    spec = stack_spec(CLAIM_ID, project, 18084)
    desired = cast("JsonObject", spec["desired"])
    desired["owned_networks"] = [
        {
            "attachable": False,
            "driver": "bridge",
            "internal": False,
            "labels": [],
            "network_name": network_name,
        }
    ]
    desired["owned_volumes"] = [
        {"driver": "local", "labels": [], "volume_name": volume_name}
    ]
    services = cast("list[JsonObject]", desired["services"])
    service = services[0]
    service["gid"] = 0
    service["network_mode"] = "bridge"
    service["network_refs"] = [{"aliases": ["db"], "network_name": network_name}]
    service["uid"] = 0
    service["volume_mounts"] = [
        {
            "read_only": False,
            "target": "/var/lib/postgresql/data",
            "volume_name": volume_name,
        }
    ]
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))
    ledger, _ = load_json(ledger_path)
    claim = cast("list[JsonObject]", ledger["claims"])[0]
    container_id = "e" * 64
    network_id = "f" * 64
    listener: JsonObject = {
        "argv_sha256": None,
        "container_id": container_id,
        "executable_realpath": None,
        "host": "127.0.0.1",
        "owner_kind": "container",
        "pid": None,
        "port": 18084,
        "process_start_ticks": None,
        "socket_inode": 123,
        "transport": "tcp",
    }
    raw_inventory: JsonObject = {
        "containers": [
            {
                "command": ["postgres"],
                "config_user": "",
                "health": "healthy",
                "id": container_id,
                "image_id": service["image_id"],
                "labels": [
                    {"name": "com.docker.compose.project", "value": project},
                    {"name": "com.docker.compose.service", "value": "db"},
                ],
                "mount_targets": ["/var/lib/postgresql/data"],
                "mounts": [
                    {
                        "read_only": False,
                        "source": volume_name,
                        "target": "/var/lib/postgresql/data",
                        "type": "volume",
                    }
                ],
                "network_attachments": [
                    {
                        "aliases": ["db"],
                        "network_id": network_id,
                        "network_name": network_name,
                    }
                ],
                "network_mode": network_name,
                "published_ports": service["published_ports"],
                "restart_count": 0,
                "state": "running",
            }
        ],
        "listeners": [listener],
        "networks": [
            {
                "attachable": False,
                "driver": "bridge",
                "id": network_id,
                "internal": False,
                "labels": [],
                "name": network_name,
            }
        ],
        "volumes": [
            {
                "created_at": "2026-08-16T00:00:00Z",
                "driver": "local",
                "labels": [],
                "mountpoint": f"/var/lib/docker/volumes/{volume_name}/_data",
                "options": [],
                "scope": "local",
                "volume_name": volume_name,
            }
        ],
    }

    # When: the production observer projects the exact live claim identity.
    observed = observe_live_stack(claim, raw_inventory)

    # Then: container, volume, network, mapping, and listener identities are bound.
    assert observed is not None
    assert observed["container_ids"] == [container_id]
    assert (
        cast("list[JsonObject]", observed["owned_volumes"])[0]["volume_name"]
        == volume_name
    )
    assert (
        cast("list[JsonObject]", observed["owned_networks"])[0]["network_id"]
        == network_id
    )
    observed_service = cast("list[JsonObject]", observed["services"])[0]
    assert observed_service["network_attachments"] == [
        {
            "aliases": ["db"],
            "network_id": network_id,
            "network_name": network_name,
        }
    ]


def test_host_state_authorizes_recorded_owned_network_names() -> None:
    # Given: one active claim records its exact Docker network name and ID.
    network_id = "f" * 64
    network: JsonObject = {
        "id": network_id,
        "labels": [],
        "name": "clinic_owned_default",
    }
    ledger: JsonObject = {
        "baseline": {
            "containers": [],
            "listeners": [],
            "networks": [],
            "volumes": [],
        },
        "claims": [
            {
                "observed": {
                    "borrowed_networks": [],
                    "borrowed_volumes": [],
                    "container_ids": [],
                    "listeners": [],
                    "owned_networks": [
                        {
                            "attachable": False,
                            "driver": "bridge",
                            "internal": False,
                            "labels": [],
                            "network_id": network_id,
                            "network_name": "clinic_owned_default",
                        }
                    ],
                    "owned_volumes": [],
                    "services": [],
                },
                "status": "active",
            }
        ],
    }
    current: JsonObject = {
        "containers": [],
        "listeners": [],
        "networks": [network],
        "volumes": [],
    }

    # When: the live-host gate compares current resources with claim authority.
    require_known_host_state(ledger, current)

    # Then: the recorded network is task-owned rather than foreign.
    assert current["networks"] == [network]


def test_reconcile_releases_an_active_stack_after_exact_absence(
    tmp_path: Path,
) -> None:
    # Given: an active stack whose complete recorded live identity is now absent.
    ledger_path = snapshot(tmp_path)
    spec = stack_spec(CLAIM_ID, "clinic_removed_stack", 18085)
    transitions = claim_transitions()
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, spec))
    observed = stack_observation(spec)
    observed_path = tmp_path / "active-stack.json"
    transitions.activate_claim(
        ledger_path,
        CLAIM_ID,
        write_immutable_json(observed_path, observed),
    )
    ledger, _ = load_json(ledger_path)
    inventory = _baseline_reader(ledger_path)()

    # When: same-boot reconciliation proves the whole stack union absent.
    reconcile_same_boot(
        ledger_path,
        inventory_reader=lambda: copy.deepcopy(inventory),
        claim_observer=lambda _claim, _inventory: None,
    )

    # Then: the active claim and its empty mutable root are released once.
    released, _ = load_json(ledger_path)
    assert released["claims"] == []
    assert not (Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID).exists()


def test_reconcile_adopts_a_complete_reserved_process_and_refreshes_it(
    tmp_path: Path,
) -> None:
    # Given: a reserved process and one exact process-owned loopback listener.
    ledger_path = snapshot(tmp_path)
    spec = process_spec(tmp_path, CLAIM_ID)
    claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))
    observed = _process_observation(spec)
    inventory = _inventory_with_listener(ledger_path, observed)

    # When: reconcile adopts it and verify performs a second live observation.
    observer = _observer(observed)
    reconcile_same_boot(
        ledger_path,
        inventory_reader=lambda: copy.deepcopy(inventory),
        claim_observer=observer,
    )
    before, _ = load_json(ledger_path)
    claim_before = cast("list[JsonObject]", before["claims"])[0]
    prior = claim_before["last_verified_at_utc"]
    verify_claim(
        ledger_path,
        CLAIM_ID,
        refresh=True,
        inventory_reader=lambda: copy.deepcopy(inventory),
        claim_observer=observer,
    )

    # Then: exact live equality advances freshness; identity drift stays immutable.
    refreshed, _ = load_json(ledger_path)
    claim = cast("list[JsonObject]", refreshed["claims"])[0]
    assert claim["observed"] == observed
    assert str(claim["last_verified_at_utc"]) > str(prior)
    drifted = copy.deepcopy(observed)
    members = cast("list[JsonObject]", drifted["members"])
    members[0]["start_ticks"] = 999
    frozen = ledger_path.read_bytes()
    with pytest.raises(IsolationError, match="drift"):
        verify_claim(
            ledger_path,
            CLAIM_ID,
            refresh=True,
            inventory_reader=lambda: copy.deepcopy(inventory),
            claim_observer=_observer(drifted),
        )
    assert ledger_path.read_bytes() == frozen


def test_reconcile_finishes_bound_candidate_publication_and_release(
    tmp_path: Path,
) -> None:
    # Given: a bound candidate envelope is published but its staging/history remain.
    ledger_path = snapshot(tmp_path)
    spec = reserve_and_activate_candidate(tmp_path, ledger_path)
    ledger, _ = load_json(ledger_path)
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    envelope = candidate_envelope(ledger)
    staged = write_staged_envelope(claim_root, envelope)
    publish_candidate_envelope(
        ledger_path,
        CLAIM_ID,
        staged,
        verifier=accepting_verifier(),
    )

    # When: same-boot reconciliation replays the bound release routine.
    reconcile_same_boot(
        ledger_path,
        inventory_reader=_baseline_reader(ledger_path),
    )

    # Then: staging is gone, history is immutable, and canonical claim is absent.
    released, _ = load_json(ledger_path)
    assert released["claims"] == []
    assert not claim_root.exists()
    desired = cast("JsonObject", spec["desired"])
    outputs = cast("list[JsonObject]", desired["published_outputs"])
    history_paths = cast("list[str]", outputs[1]["relative_paths"])
    history = Path(str(outputs[1]["root_path"])) / history_paths[0]
    assert history.is_file()
    assert history.stat().st_mode & 0o777 == 0o400


def _observer(observed: JsonObject) -> ClaimObserver:
    return lambda _claim, _inventory: copy.deepcopy(observed)


def _inventory_with_stack(
    ledger_path: Path,
    observed: JsonObject,
    project: str,
) -> JsonObject:
    inventory = _baseline_reader(ledger_path)()
    service = cast("list[JsonObject]", observed["services"])[0]
    inventory["containers"] = [
        {
            "config_user": f"{service['uid']}:{service['gid']}",
            "health": None,
            "id": service["container_id"],
            "image_id": service["image_id"],
            "labels": [
                {"name": "com.docker.compose.project", "value": project},
                {"name": "com.docker.compose.service", "value": service["name"]},
            ],
            "mount_targets": [],
            "network_mode": service["network_mode"],
            "published_ports": service["published_ports"],
            "restart_count": 0,
            "state": service["state"],
        }
    ]
    inventory["listeners"] = copy.deepcopy(observed["listeners"])
    return inventory


def _process_observation(spec: JsonObject) -> JsonObject:
    desired = cast("JsonObject", spec["desired"])
    member: JsonObject = {
        "argv_sha256": desired["argv_sha256"],
        "executable_realpath": desired["interpreter_realpath"],
        "gid": desired["gid"],
        "pgid": 123,
        "pid": 123,
        "ppid": 1,
        "sid": 123,
        "start_ticks": 456,
        "uid": desired["uid"],
    }
    listener: JsonObject = {
        "argv_sha256": member["argv_sha256"],
        "container_id": None,
        "executable_realpath": member["executable_realpath"],
        "host": "127.0.0.1",
        "owner_kind": "process",
        "pid": 123,
        "port": 18080,
        "process_start_ticks": 456,
        "socket_inode": 4242,
        "transport": "tcp",
    }
    return {
        "listener_socket_inode": 4242,
        "listeners": [listener],
        "members": [member],
    }


def _inventory_with_listener(ledger_path: Path, observed: JsonObject) -> JsonObject:
    inventory = _baseline_reader(ledger_path)()
    inventory["listeners"] = copy.deepcopy(observed["listeners"])
    return inventory


def _baseline_reader(ledger_path: Path) -> Callable[[], JsonObject]:
    ledger, _ = load_json(ledger_path)
    baseline = cast("JsonObject", ledger["baseline"])
    inventory: JsonObject = {
        key: copy.deepcopy(baseline[key])
        for key in ("containers", "listeners", "networks", "volumes")
    }
    return lambda: copy.deepcopy(inventory)
