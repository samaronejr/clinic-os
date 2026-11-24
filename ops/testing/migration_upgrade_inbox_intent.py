"""Compile native inbox outcomes independently from the fixture step scripts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ops.testing.migration_upgrade_inbox_fixture import CASE_STEPS, TIMESTAMP
from ops.testing.migration_upgrade_rows import Row, Tables, expected_rows

if TYPE_CHECKING:
    from collections.abc import Mapping

_MIGRATIONS = {
    ("comms", "0012_attempt_delivery"),
    ("comms", "0013_attempt_sequence"),
    ("comms", "0015_send_permit"),
    ("comms", "0016_permit_evidence"),
    ("comms", "0017_inflight_claim"),
    ("comms", "0018_session_claim"),
}
_RANK = {"": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 0}


def _key(value: str) -> str:
    return json.dumps([value], separators=(",", ":"))


def _report(current: str, incoming: str) -> str:
    if current == "failed":
        return incoming
    if incoming == "failed":
        return current if current in {"read", "delivered"} else incoming
    return incoming if _RANK[incoming] > _RANK[current] else current


def project_case(
    name: str, ids: Mapping[str, str], account: str, tenant: str
) -> tuple[str, dict[str, Row], dict[str, Row]]:
    """Derive attempt and provider receipt values without reading either."""
    channel, steps = CASE_STEPS[name]
    message = ids["message"]
    conversation = ids["conversation"]
    attempts: dict[str, Row] = {}
    receipts: dict[str, Row] = {}
    closed = False
    state = ""
    for verb, *arguments in steps:
        if verb == "close":
            closed = True
        elif verb == "send" or verb.startswith("retry"):
            label = "abc"[len(attempts)]
            attempts[label] = {
                "id": ids[label],
                "organization_id": tenant,
                "channel": channel,
                "subject_type": "comms.message",
                "subject_id": message,
                "idempotency_key": label,
                "status": "pending",
                "provider_reference": None,
            }
        elif verb == "dispatch":
            label = arguments[0]
            if closed:
                attempts[label]["status"] = "cancelled"
            else:
                attempts[label]["status"] = "succeeded"
                attempts[label]["provider_reference"] = (
                    f"synthetic:{channel}:{ids[label]}"
                )
        elif verb == "fault":
            label, fault = arguments
            attempts[label]["status"] = (
                "failed" if fault == "rejected" else "in_progress"
            )
        elif verb == "status":
            label, report, *timestamp = arguments
            state = _report(state, report)
            if channel == "whatsapp":
                reference = attempts[label]["provider_reference"]
                event = (
                    f"{reference}:{report}:{timestamp[0] if timestamp else TIMESTAMP}"
                )
                receipts[event] = {
                    "organization_id": tenant,
                    "provider_account_id": account,
                    "event_id": event,
                    "kind": "status",
                }
    graph: Tables = {
        "comms_integrationoperation": {_key(ids[k]): v for k, v in attempts.items()},
        "comms_inboundevent": receipts,
        "comms_message": {
            _key(message): {
                "conversation_id": conversation,
                "command_id": "a",
                "provider_state": state,
            },
        },
        "comms_conversation": {_key(conversation): {"provider_account_id": account}},
    }
    expected_rows(graph, _MIGRATIONS)
    return state, attempts, receipts
