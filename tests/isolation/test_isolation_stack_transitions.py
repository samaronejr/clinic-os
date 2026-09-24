from __future__ import annotations

from pathlib import Path

import pytest
from ops.testing.isolation_common import IsolationError, load_json

from isolation_claim_fixtures import (
    CLAIM_ID,
    SECOND_CLAIM_ID,
    STACK_PORT,
    claim_transitions,
    snapshot,
    stack_observation,
    stack_spec,
    write_immutable_json,
    write_spec,
)


def test_stack_reservation_requires_a_nonempty_service_set(tmp_path: Path) -> None:
    # Given: an open ledger and a closed stack spec with no desired service.
    ledger_path = snapshot(tmp_path)
    spec = stack_spec(CLAIM_ID, "clinic_phase1a_empty", STACK_PORT)
    desired = spec["desired"]
    assert isinstance(desired, dict)
    desired["services"] = []
    spec_path = write_spec(tmp_path, spec)
    before = ledger_path.read_bytes()

    # When: the claim transition parses the service-less stack.
    with pytest.raises(IsolationError, match="service"):
        claim_transitions().reserve_claim(ledger_path, spec_path)

    # Then: no ledger bytes or resource root are reserved.
    assert ledger_path.read_bytes() == before


def test_stack_reservation_records_owned_docker_resources_and_service_mappings(
    tmp_path: Path,
) -> None:
    # Given: an isolated PostgreSQL spec owning one network and one data volume.
    ledger_path = snapshot(tmp_path)
    spec = stack_spec(CLAIM_ID, "clinic_phase1a_resources", STACK_PORT)
    desired = spec["desired"]
    assert isinstance(desired, dict)
    desired["owned_networks"] = [
        {
            "attachable": False,
            "driver": "bridge",
            "internal": False,
            "labels": [],
            "network_name": "clinic_phase1a_resources_default",
        }
    ]
    desired["owned_volumes"] = [
        {
            "driver": "local",
            "labels": [],
            "volume_name": "clinic_phase1a_resources_data",
        }
    ]
    services = desired["services"]
    assert isinstance(services, list)
    assert isinstance(services[0], dict)
    services[0]["network_mode"] = "bridge"
    services[0]["network_refs"] = [
        {
            "aliases": ["db"],
            "network_name": "clinic_phase1a_resources_default",
        }
    ]
    services[0]["volume_mounts"] = [
        {
            "read_only": False,
            "target": "/var/lib/postgresql/data",
            "volume_name": "clinic_phase1a_resources_data",
        }
    ]

    # When: the stable-lock transition reserves every Docker identity first.
    reserved = claim_transitions().reserve_claim(
        ledger_path,
        write_spec(tmp_path, spec),
    )

    # Then: the exact resourceful desired mapping is retained under one claim.
    ledger, _ = load_json(ledger_path)
    claims = ledger["claims"]
    assert reserved == CLAIM_ID
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    assert claims[0]["desired"] == desired


def test_stack_reservation_rejects_duplicate_loopback_port_ownership(
    tmp_path: Path,
) -> None:
    # Given: one reserved stack already owns the requested loopback port.
    ledger_path = snapshot(tmp_path)
    transitions = claim_transitions()
    first = stack_spec(CLAIM_ID, "clinic_phase1a_first", STACK_PORT)
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, first))
    second = stack_spec(SECOND_CLAIM_ID, "clinic_phase1a_second", STACK_PORT)
    second_path = write_spec(tmp_path, second)
    before = ledger_path.read_bytes()

    # When: a second stack attempts to reserve the same exact endpoint.
    with pytest.raises(IsolationError, match="collision"):
        transitions.reserve_claim(ledger_path, second_path)

    # Then: the first authority remains and the second root stays absent.
    ledger, _ = load_json(ledger_path)
    claims = ledger["claims"]
    assert ledger_path.read_bytes() == before
    assert isinstance(claims, list)
    assert [claim["claim_id"] for claim in claims if isinstance(claim, dict)] == [
        CLAIM_ID
    ]
    second_root = Path(str(ledger["attempt_root"])) / "claims" / SECOND_CLAIM_ID
    assert not second_root.exists()


def test_stack_activation_records_exact_observed_service_mapping(
    tmp_path: Path,
) -> None:
    # Given: a reserved stack and one immutable complete observation fixture.
    ledger_path = snapshot(tmp_path)
    transitions = claim_transitions()
    spec = stack_spec(CLAIM_ID, "clinic_phase1a_active", STACK_PORT)
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, spec))
    observed = stack_observation(spec)
    observed_path = write_immutable_json(tmp_path / "stack-observed.json", observed)

    # When: activation validates every service, container, listener, and mapping.
    transitions.activate_claim(ledger_path, CLAIM_ID, observed_path)

    # Then: the stack becomes active with exactly the immutable observed bytes.
    activated, _ = load_json(ledger_path)
    claims = activated["claims"]
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    assert claims[0]["status"] == "active"
    assert claims[0]["observed"] == observed
    assert claims[0]["activated_at_utc"] == activated["last_verified_at_utc"]


def test_stack_activation_rejects_a_service_mapping_drift_without_mutation(
    tmp_path: Path,
) -> None:
    # Given: a reserved stack whose observation drifts from its desired command.
    ledger_path = snapshot(tmp_path)
    transitions = claim_transitions()
    spec = stack_spec(CLAIM_ID, "clinic_phase1a_drift", STACK_PORT)
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, spec))
    observed = stack_observation(spec)
    services = observed["services"]
    assert isinstance(services, list)
    assert isinstance(services[0], dict)
    services[0]["command"] = ["postgres", "--unsafe-drift"]
    observed_path = write_immutable_json(tmp_path / "stack-drift.json", observed)
    before = ledger_path.read_bytes()

    # When: activation compares the claimed mapping with the desired mapping.
    with pytest.raises(IsolationError, match="mapping"):
        transitions.activate_claim(ledger_path, CLAIM_ID, observed_path)

    # Then: the rejected observation leaves the canonical ledger byte-identical.
    assert ledger_path.read_bytes() == before


def test_stack_release_refuses_live_identities_but_allows_empty_reservation(
    tmp_path: Path,
) -> None:
    # Given: one active stack and one reserved stack with private roots.
    ledger_path = snapshot(tmp_path)
    transitions = claim_transitions()
    active_spec = stack_spec(CLAIM_ID, "clinic_phase1a_live", STACK_PORT)
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, active_spec))
    active_observed = write_immutable_json(
        tmp_path / "stack-live.json",
        stack_observation(active_spec),
    )
    transitions.activate_claim(ledger_path, CLAIM_ID, active_observed)
    reserved_spec = stack_spec(
        SECOND_CLAIM_ID,
        "clinic_phase1a_reserved",
        STACK_PORT + 1,
    )
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, reserved_spec))
    ledger, _ = load_json(ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    (attempt_root / "claims" / CLAIM_ID).rmdir()
    (attempt_root / "claims" / SECOND_CLAIM_ID).rmdir()
    before = ledger_path.read_bytes()

    # When: release checks the recorded identity set before removing authority.
    with pytest.raises(IsolationError, match="live stack"):
        transitions.release_claim(ledger_path, CLAIM_ID)
    assert ledger_path.read_bytes() == before
    transitions.release_claim(ledger_path, SECOND_CLAIM_ID)

    # Then: only the identity-free reserved stack is removed from authority.
    released, _ = load_json(ledger_path)
    claims = released["claims"]
    assert isinstance(claims, list)
    assert [claim["claim_id"] for claim in claims if isinstance(claim, dict)] == [
        CLAIM_ID
    ]
