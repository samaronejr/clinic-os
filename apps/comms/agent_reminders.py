"""Offline logistics-reminder command for the agent execution rehearsal."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid5

from config.settings.contracts import SYNTHETIC_DATA_MODE
from django.conf import settings
from django.db import connections

from apps.ai.action_contracts import ActionRefusedError, Refusal
from apps.ai.attestations import attest, verify

if TYPE_CHECKING:
    from apps.ai.action_payload import JSON

MAX_SIMULATED_EFFECTS: Final = 1024
PROVIDER: Final = "synthetic"
_EFFECTS: OrderedDict[UUID, ReminderOutcome] = OrderedDict()
_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class ReminderOutcome:
    """A content-free authenticated fake-provider result."""

    provider_key: UUID
    reference: UUID
    outcome: Literal["succeeded", "failed", "unknown", "cancelled"]
    attestation: str

    def value(self) -> dict[str, JSON]:
        """Bind provider reconciliation to the original idempotency key."""
        return {
            "provider_key": str(self.provider_key),
            "reference": str(self.reference),
            "outcome": self.outcome,
            "key_id": "agent-attestation-v1",
        }


def _outcome(
    key: UUID, outcome: Literal["succeeded", "failed", "unknown", "cancelled"]
) -> ReminderOutcome:
    reference = uuid5(key, "synthetic-logistics-v1")
    result = ReminderOutcome(key, reference, outcome, "")
    return ReminderOutcome(
        key,
        reference,
        outcome,
        attest(purpose="clinic-agent-synthetic-provider-v1", value=result.value()),
    )


def send_reminder(*, provider_key: UUID, template: str) -> ReminderOutcome:
    """Simulate acceptance; no network, destination, clinical text or paid service."""
    if (
        settings.CLINIC_DATA_MODE != SYNTHETIC_DATA_MODE
        or template != "logistics-v1"
        or any(
            database.in_atomic_block
            for database in connections.all(initialized_only=True)
        )
    ):
        raise ActionRefusedError(Refusal.TOOL)
    with _LOCK:
        if provider_key not in _EFFECTS:
            _EFFECTS[provider_key] = _outcome(provider_key, "succeeded")
            if len(_EFFECTS) > MAX_SIMULATED_EFFECTS:
                _EFFECTS.popitem(last=False)
        return _EFFECTS[provider_key]


def reconcile_reminder(*, provider_key: UUID) -> ReminderOutcome:
    """Query the deterministic simulated provider identity; never resend."""
    if settings.CLINIC_DATA_MODE != SYNTHETIC_DATA_MODE:
        raise ActionRefusedError(Refusal.TOOL)
    with _LOCK:
        return _EFFECTS.get(provider_key) or _outcome(provider_key, "unknown")


def cancel_reminder(*, provider_key: UUID, original_key: UUID) -> ReminderOutcome:
    """Simulate the declared cancellation, not reversal of any clinical action."""
    if settings.CLINIC_DATA_MODE != SYNTHETIC_DATA_MODE:
        raise ActionRefusedError(Refusal.TOOL)
    with _LOCK:
        if original_key not in _EFFECTS:
            return _outcome(provider_key, "unknown")
        _EFFECTS[original_key] = _outcome(original_key, "cancelled")
        return _outcome(provider_key, "cancelled")


def authenticate_outcome(*, result: ReminderOutcome, provider_key: UUID) -> None:
    """Do not persist a forged result or accept another operation's receipt."""
    if result.provider_key != provider_key:
        raise ActionRefusedError(Refusal.ATTESTATION)
    verify(
        purpose="clinic-agent-synthetic-provider-v1",
        value=result.value(),
        signature=result.attestation,
    )
