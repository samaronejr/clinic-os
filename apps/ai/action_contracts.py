"""Closed agent outcomes; no rejected payload is rendered in an exception."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID


class ActionInputError(ValueError):
    """Reject an ambiguous or unregistered action."""

    def __init__(self) -> None:
        """Expose only a fixed boundary error."""
        super().__init__("invalid agent action")


class ActionAccessDeniedError(PermissionError):
    """Unknown and foreign selectors have the same refusal."""

    def __init__(self) -> None:
        """Do not disclose a selector or its existence."""
        super().__init__("agent action access denied")


class Refusal(StrEnum):
    """Stable, content-free execution decisions."""

    DIGEST = "digest_mismatch"
    ATTESTATION = "invalid_attestation"
    AUTHORITY = "authority_revoked"
    EXPIRED = "expired"
    EARLY = "not_before"
    SOURCE = "source_superseded"
    POLICY = "policy_changed"
    DISABLED = "disabled"
    BUDGET = "budget_exhausted"
    TOOL = "tool_forbidden"
    QUORUM = "approval_required"
    STATE = "state_conflict"
    INJECTION = "untrusted_instruction"
    FENCE = "stale_fence"
    OUTCOME = "unknown_outcome"


class ActionRefusedError(RuntimeError):
    """Carry a closed reason, never source content or provider exceptions."""

    def __init__(self, reason: Refusal) -> None:
        """Preserve the machine-consumed refusal code."""
        self.reason = reason
        super().__init__(reason.value)


class ActionState(StrEnum):
    """The persisted agent-action state machine."""

    PROPOSAL = "proposal"
    VALIDATED = "validated"
    REJECTED = "rejected"
    APPROVED = "approved"
    EXECUTING = "executing"
    RECEIPT = "receipt"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"
    EXCEPTION = "exception"
    FAILURE = "failure"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    MANUAL_REQUIRED = "manual_required"


TRANSITIONS: Final = {
    "proposal": frozenset({"validated", "rejected"}),
    "validated": frozenset({"approved", "rejected"}),
    "approved": frozenset({"executing", "rejected"}),
    "executing": frozenset({"receipt", "unknown", "failure", "rejected"}),
    "unknown": frozenset({"reconciled", "exception"}),
    "failure": frozenset({"compensating"}),
    "compensating": frozenset({"compensated", "manual_required"}),
}
AUTONOMY: Final = ("A0", "A1", "A2", "A3")
OPERATIONS: Final = ("comms.synthetic_reminder", "workflows.review_task")
MAX_ACTION_BYTES: Final = 65_536


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """A visible result names retained evidence, never a provider body."""

    proposal_id: UUID
    state: str
    execution_id: UUID | None = None
    receipt_id: UUID | None = None
    reason: Refusal | None = None
