"""Closed, non-identifying refusals of the interop domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

SOURCE_SUPERSEDED: Final = "source_superseded"
NOT_ACKNOWLEDGED: Final = "not_acknowledged"
CONSENT_REQUIRED: Final = "consent_required"
IDENTIFIER_REQUIRED: Final = "identifier_required"
ALREADY_SUBMITTED: Final = "already_submitted"
RNDS_INELIGIBLE_REASONS: Final = (
    SOURCE_SUPERSEDED,
    NOT_ACKNOWLEDGED,
    CONSENT_REQUIRED,
    IDENTIFIER_REQUIRED,
    ALREADY_SUBMITTED,
)


class InteropAccessDeniedError(Exception):
    """One denial for unknown, foreign and unauthorized selectors alike."""


class InteropConflictError(Exception):
    """A stale revision, reused command or terminal record; nothing is reflected."""


class InteropInputError(ValueError):
    """Input outside the bounded management, partner or export contract."""


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One failing element path and a closed issue code; never the value."""

    path: str
    code: str


class RndsValidationError(InteropInputError):
    """The exam document or bundle breaks the recorded RNDS profile rules."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        """Carry every issue so a caller can surface all of them at once."""
        super().__init__("rnds validation failed")
        self.issues = issues


class RndsIneligibleError(Exception):
    """The visible record cannot be shared with RNDS now; the reason is closed."""

    def __init__(self, reason: str) -> None:
        """Bind one reason from ``RNDS_INELIGIBLE_REASONS``."""
        if reason not in RNDS_INELIGIBLE_REASONS:
            raise ValueError(reason)
        super().__init__(reason)
        self.reason = reason


class RndsUnavailableError(Exception):
    """No RNDS leg may run: EG-7 is unmet, or the fixture rehearsal is disabled."""
