"""Build synthetic provider status reports without importing fixture writers."""

import json
from uuid import uuid4

from ops.testing.migration_upgrade_inbox_fixture import ACCOUNTS


def status_body(channel: str, reference: str, status: str, stamp: str) -> bytes:
    """Encode one status report for the declared synthetic provider account."""
    if channel != "whatsapp":
        return json.dumps(
            {
                "account": ACCOUNTS[channel][1],
                "events": [
                    {
                        "kind": "status",
                        "event_id": f"upgrade-status-{uuid4().hex}",
                        "provider_message_id": reference,
                        "status": status,
                        "sent_at": int(stamp),
                    }
                ],
            }
        ).encode()
    value = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "551100000000",
            "phone_number_id": ACCOUNTS[channel][1],
        },
        "contacts": [
            {"profile": {"name": "Paciente Sintetico"}, "wa_id": "5511999990900"}
        ],
        "messages": [],
        "statuses": [
            {
                "id": reference,
                "status": status,
                "timestamp": stamp,
                "recipient_id": "5511999990900",
                "pricing": {
                    "billable": True,
                    "pricing_model": "PMP",
                    "type": "regular",
                    "category": "utility",
                },
            }
        ],
    }
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-upgrade",
                    "changes": [{"field": "messages", "value": value}],
                }
            ],
        }
    ).encode()
