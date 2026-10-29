"""Trusted synchronous offboarding extensions, empty in todo 15.

The offboarding service owns actor/clinic checks, final-owner and remaining-work
guards, the transaction, and core revocation. A handler runs in that established
context before the remaining-work check and revocation, never after commit.
Todo 26 adds its task-transfer handler; no downstream handler is installed here.
"""

from __future__ import annotations

from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID


class DuplicateOffboardingHandlerError(ValueError):
    """Reject running the same registered handler twice in a command."""


class OffboardingHandler(Protocol):
    """Handle open work in the caller's authorized tenant transaction."""

    def __call__(self, *, clinic_id: UUID, user_id: UUID) -> None:
        """Complete synchronously or raise to roll back the whole command."""
        ...


_handlers: list[OffboardingHandler] = []


def register_offboarding_handler(handler: OffboardingHandler) -> None:
    """Register a trusted callback once, without executing it."""
    if not callable(handler) or iscoroutinefunction(handler):
        message = "Offboarding handlers must be synchronous"
        raise TypeError(message)
    if any(registered is handler for registered in _handlers):
        raise DuplicateOffboardingHandlerError
    _handlers.append(handler)


def registered_offboarding_handlers() -> tuple[OffboardingHandler, ...]:
    """Return an immutable registration-order snapshot of installed handlers."""
    return tuple(_handlers)
