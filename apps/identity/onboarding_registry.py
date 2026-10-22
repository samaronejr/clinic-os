"""Trusted startup declarations for permission-filtered onboarding steps.

Registration grants no authority. The onboarding service checks the current
actor's clinic scope and the declared permission before running a read-only
completion check in that same transaction. None means no additional clinic
permission, not an anonymous or unscoped caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from uuid import UUID


MAX_STEP_KEY_LENGTH: Final = 80


class DuplicateOnboardingStepError(ValueError):
    """Reject replacing a registered step or its authorization declaration."""


class CompletionCheck(Protocol):
    """Read whether a step is complete in the authorized current transaction."""

    def __call__(self, *, clinic_id: UUID) -> bool:
        """Return an actual bool without changing domain state."""
        ...


@dataclass(frozen=True, slots=True)
class OnboardingStep:
    """Keep a stable key, permission and synchronous completion callback."""

    key: str
    permission: str | None
    completion_check: CompletionCheck


_steps: dict[str, OnboardingStep] = {}


def register_onboarding_step(
    key: str,
    permission: str | None,
    completion_check: CompletionCheck,
) -> None:
    """Register one trusted step without executing its completion check."""
    if not key or key != key.strip() or len(key) > MAX_STEP_KEY_LENGTH:
        message = "Invalid onboarding step key"
        raise ValueError(message)
    if permission is not None and (not permission or permission != permission.strip()):
        message = "Invalid onboarding permission"
        raise ValueError(message)
    if not callable(completion_check) or iscoroutinefunction(completion_check):
        message = "Onboarding completion checks must be synchronous"
        raise TypeError(message)
    if key in _steps:
        raise DuplicateOnboardingStepError
    _steps[key] = OnboardingStep(key, permission, completion_check)


def registered_onboarding_steps() -> tuple[OnboardingStep, ...]:
    """Return an immutable startup-order snapshot, not authorized progress."""
    return tuple(_steps.values())
