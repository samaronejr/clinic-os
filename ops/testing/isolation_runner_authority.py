"""Bind immutable runner intent fields to one current claim."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Never

import rfc8785

from ops.testing.isolation_common import IsolationError, JsonObject, raw_sha256
from ops.testing.isolation_runner_create import runner_create_argv_sha
from ops.testing.isolation_runner_records import runner_creation, runner_service
from ops.testing.isolation_runner_validation import runner_labels


@dataclass(frozen=True, slots=True)
class RunnerAuthority:
    """Carry one fully reauthenticated runner ownership record."""

    ledger: JsonObject
    claim: JsonObject
    service: JsonObject
    creation: JsonObject
    labels: dict[str, str]


def authenticate_runner_authority(
    ledger: JsonObject,
    claim: JsonObject,
) -> RunnerAuthority:
    """Recompute every immutable intent binding before Docker access."""
    service = runner_service(claim)
    creation = runner_creation(claim)
    container_name = f"clinic-phase1a-runner-{claim.get('claim_id')}"
    create_sha = runner_create_argv_sha(ledger, claim, service, container_name)
    core: JsonObject = {
        "attempt_id": ledger["attempt_id"],
        "claim_id": claim["claim_id"],
        "container_name": container_name,
        "create_argv_sha256": create_sha,
        "creation_boot_id": ledger["boot_id"],
        "desired_service_sha256": raw_sha256(rfc8785.dumps(service)),
        "intent_id": creation["intent_id"],
        "schema_version": 1,
        "service_name": service["name"],
    }
    stored = set(core) - {"attempt_id", "claim_id"}
    if any(creation.get(key) != core[key] for key in stored):
        _fail("runner creation binding drifted")
    if creation.get("intent_sha256") != raw_sha256(rfc8785.dumps(core)):
        _fail("runner intent hash drifted")
    return RunnerAuthority(
        ledger,
        claim,
        service,
        creation,
        runner_labels(ledger, claim, creation),
    )


def _fail(message: str) -> Never:
    raise IsolationError(message)
