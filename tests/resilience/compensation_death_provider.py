"""Drive the compensation service at either side of a durable synthetic cancel."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from apps.ai.action_recovery import compensate_action
from apps.comms import agent_reminders
from django.db import connections

from resilience.broker_death_provider import _hold_after_acceptance
from resilience.death_worker import record_effect


def run_compensation(*, clinic_id: str, operation_id: str, principal_id: str) -> str:
    """The subprocess uses the test's isolated, authenticated agent login."""
    connections["agent"].settings_dict["PASSWORD"] = os.environ[
        "CLINIC_GATE_AGENT_PASSWORD"
    ]
    return compensate_action(
        clinic_id=UUID(clinic_id),
        proposal_id=UUID(operation_id),
        principal_id=UUID(principal_id),
    ).state


def cancel(
    *, provider_key: UUID, original_key: UUID
) -> agent_reminders.ReminderOutcome:
    ledger = Path(os.environ["CLINIC_GATE_CANCEL_LEDGER"])
    if os.environ["CLINIC_GATE_CANCEL_CUT"] == "before":
        _hold_after_acceptance()
    record_effect(ledger, f"{provider_key} {original_key}")
    _hold_after_acceptance()
    raise AssertionError


def lookup(*, provider_key: UUID) -> agent_reminders.ReminderOutcome:
    ledger = Path(os.environ["CLINIC_GATE_CANCEL_LEDGER"])
    record_effect(ledger.with_suffix(".lookups"), str(provider_key))
    found = ledger.exists() and any(
        row.split()[0] == str(provider_key) for row in ledger.read_text().splitlines()
    )
    return agent_reminders._outcome(provider_key, "cancelled" if found else "unknown")
