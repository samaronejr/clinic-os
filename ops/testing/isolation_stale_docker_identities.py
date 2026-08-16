"""Project exact Docker ownership labels into stale action identities."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue


def runner_labels(
    attempt_id: str,
    claim_id: str,
    runner: JsonObject,
) -> JsonValue:
    """Bind the deterministic runner selector labels without reading secrets."""
    labels = [
        {"name": "clinic.phase1a.attempt", "value": attempt_id},
        {"name": "clinic.phase1a.claim", "value": claim_id},
        {
            "name": "clinic.phase1a.runner-create-intent",
            "value": runner.get("intent_sha256"),
        },
        {"name": "clinic.phase1a.service", "value": runner.get("service_name")},
    ]
    return cast("JsonValue", labels)
