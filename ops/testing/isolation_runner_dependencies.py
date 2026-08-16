"""Guard runner cleanup against live reverse dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never, cast

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue


def require_no_runner_reverse_dependency(
    claims: list[JsonObject],
    claim: JsonObject,
) -> None:
    """Reject cleanup while another live claim names this runner."""
    identifier = claim.get("claim_id")
    if any(
        isinstance(item.get("dependency_claim_ids"), list)
        and identifier in cast("list[JsonValue]", item["dependency_claim_ids"])
        for item in claims
    ):
        _fail("runner claim has a reverse dependency")


def _fail(message: str) -> Never:
    raise IsolationError(message)
