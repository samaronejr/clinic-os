"""Derive full physical identities for ordinary stale claims."""

from __future__ import annotations

import copy
import hashlib
from typing import TYPE_CHECKING, Never, cast

import rfc8785

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_stale_docker_identities import runner_labels

if TYPE_CHECKING:
    from pathlib import Path


def build_claim_action_identities(
    attempt_id: str,
    attempt_root: Path,
    claim: JsonObject,
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Return ordered identities and copied runner recovery authority."""
    claim_id = _text(claim.get("claim_id"), "claim ID")
    origin = _digest_source(claim)
    kind = claim.get("kind")
    identities: list[JsonObject] = []
    recoveries: list[JsonObject] = []
    if kind == "process":
        identities.append(_process_identity(attempt_id, claim, claim_id, origin))
    elif kind == "stack":
        stack_identities, stack_recoveries = _stack_identities(
            attempt_id,
            claim,
            claim_id,
            origin,
        )
        identities.extend(stack_identities)
        recoveries.extend(stack_recoveries)
    elif kind != "filesystem":
        _fail("stale action claim kind is invalid")
    identities.append(
        _staging_identity(attempt_id, attempt_root, claim, claim_id, origin)
    )
    return identities, recoveries


def _process_identity(
    attempt_id: str,
    claim: JsonObject,
    claim_id: str,
    origin: str,
) -> JsonObject:
    observed = _object(claim.get("observed"), "process observation")
    return {
        "action_id": f"process-absent-{claim_id}",
        "attempt_id": attempt_id,
        "claim_id": claim_id,
        "observed_listeners": copy.deepcopy(observed.get("listeners", [])),
        "observed_members": copy.deepcopy(observed.get("members", [])),
        "operation": "observe-prior-boot-process-absent",
        "origin_claim_sha256": origin,
        "resource_kind": "process",
        "schema_version": 1,
    }


def _stack_identities(
    attempt_id: str,
    claim: JsonObject,
    claim_id: str,
    origin: str,
) -> tuple[list[JsonObject], list[JsonObject]]:
    identities: list[JsonObject] = []
    recoveries: list[JsonObject] = []
    observed = _object(claim.get("observed"), "stack observation")
    desired = _object(claim.get("desired"), "stack desired")
    runner = claim.get("runner_creation")
    if isinstance(runner, dict) and runner.get("state") != "removed":
        action_id = f"container-remove-{claim_id}"
        identity: JsonObject = {
            "action_id": action_id,
            "attempt_id": attempt_id,
            "claim_id": claim_id,
            "container_id": runner.get("container_id"),
            "container_name": runner.get("container_name"),
            "expected_labels": runner_labels(attempt_id, claim_id, runner),
            "expected_labels_exact": True,
            "operation": "stop-remove-container",
            "origin_claim_sha256": origin,
            "resource_kind": "container",
            "runner_creation": copy.deepcopy(runner),
            "schema_version": 1,
        }
        identities.append(identity)
        recoveries.append(
            {
                "action_id": action_id,
                "claim_id": claim_id,
                "origin_claim_sha256": origin,
                "origin_status": claim.get("status"),
                "runner_creation": copy.deepcopy(runner),
            }
        )
    else:
        identities.extend(
            _service_identities(attempt_id, claim_id, origin, desired, observed)
        )
    identities.extend(_resource_identities(attempt_id, claim_id, origin, observed))
    return identities, recoveries


def _service_identities(
    attempt_id: str,
    claim_id: str,
    origin: str,
    desired: JsonObject,
    observed: JsonObject,
) -> list[JsonObject]:
    services = sorted(
        _objects(observed.get("services", []), "observed services"),
        key=lambda item: str(item.get("container_id")),
    )
    return [
        {
            "action_id": (
                f"container-remove-{claim_id}-"
                f"{_text(service.get('container_id'), 'container ID')[:12]}"
            ),
            "attempt_id": attempt_id,
            "claim_id": claim_id,
            "container_id": service.get("container_id"),
            "expected_labels": [
                {
                    "name": "com.docker.compose.project",
                    "value": desired.get("project"),
                },
                {
                    "name": "com.docker.compose.service",
                    "value": service.get("name"),
                },
            ],
            "expected_labels_exact": False,
            "observed_service": copy.deepcopy(service),
            "operation": "stop-remove-container",
            "origin_claim_sha256": origin,
            "resource_kind": "container",
            "schema_version": 1,
        }
        for service in services
    ]


def _resource_identities(
    attempt_id: str,
    claim_id: str,
    origin: str,
    observed: JsonObject,
) -> list[JsonObject]:
    result: list[JsonObject] = []
    fields = (
        ("borrowed_networks", "borrowed-network-attachment", "detach-borrowed-network"),
        ("owned_networks", "owned-network", "remove-owned-network"),
        ("owned_volumes", "owned-volume", "remove-owned-volume"),
    )
    for field, resource_kind, operation in fields:
        resources = _objects(observed.get(field, []), field)
        for index, resource in enumerate(resources):
            name = resource.get("network_name", resource.get("volume_name", index))
            result.append(
                {
                    "action_id": f"{resource_kind}-remove-{claim_id}-{name}",
                    "attempt_id": attempt_id,
                    "claim_id": claim_id,
                    **(
                        {"container_ids": copy.deepcopy(observed.get("container_ids"))}
                        if operation == "detach-borrowed-network"
                        else {}
                    ),
                    "observed_resource": copy.deepcopy(resource),
                    "operation": operation,
                    "origin_claim_sha256": origin,
                    "resource_kind": resource_kind,
                    "schema_version": 1,
                }
            )
    return result


def _staging_identity(
    attempt_id: str,
    attempt_root: Path,
    claim: JsonObject,
    claim_id: str,
    origin: str,
) -> JsonObject:
    desired = _object(claim.get("desired"), "claim desired")
    observed = _object(claim.get("observed"), "claim observation")
    return {
        "action_id": f"staging-remove-{claim_id}",
        "attempt_id": attempt_id,
        "claim_id": claim_id,
        "claim_root_path": str(attempt_root / str(claim.get("root_relative_path"))),
        "desired_owned_files": copy.deepcopy(desired.get("owned_files", [])),
        "observed_owned_files": copy.deepcopy(observed.get("owned_files", [])),
        "operation": "remove-staging",
        "origin_claim_sha256": origin,
        "resource_kind": "filesystem-staging",
        "schema_version": 1,
    }


def _digest_source(claim: JsonObject) -> str:
    return hashlib.sha256(rfc8785.dumps(claim)).hexdigest()


def _fail(message: str) -> Never:
    raise IsolationError(message)


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
