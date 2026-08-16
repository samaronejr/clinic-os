"""Route same-boot stack and process claims to live host observers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ops.testing.isolation_common import JsonObject
from ops.testing.isolation_process_live import observe_live_process
from ops.testing.isolation_stack_live import observe_live_stack

if TYPE_CHECKING:
    from collections.abc import Callable

type ClaimObserver = Callable[[JsonObject, JsonObject], JsonObject | None]


def observe_live_claim(
    claim: JsonObject,
    inventory: JsonObject,
) -> JsonObject | None:
    """Observe only supported live claim kinds without trusting stored bytes."""
    kind = claim.get("kind")
    if kind == "stack":
        return observe_live_stack(claim, inventory)
    if kind == "process":
        return observe_live_process(claim, inventory)
    return None
