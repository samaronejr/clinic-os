"""Synthetic provider accounts, signed webhook bodies and staff worlds for the inbox."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.comms.inbound_providers import sign
from apps.comms.inbox_inbound import InboundReceipt, receive_inbound, secret_name
from apps.comms.models import ProviderAccount
from apps.identity.models import ServicePrincipal, UserClinicRole

from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

PHONE_NUMBER_ID = "100000000000001"
CONTACT = "5511999990001"
TIMESTAMP = "1790000000"


_MANAGERS: dict[UUID, UUID] = {}


def manager_of(graph: RbacGraph) -> UUID:
    """A clinic admin who holds the role in clinic A.

    The graph's own ``clinic_admin`` administers clinic B, so it is refused by
    every clinic-A inbox command, which is the behaviour under test elsewhere.
    """
    if graph.clinic_a not in _MANAGERS:
        _MANAGERS[graph.clinic_a] = create_role_actor(
            graph, UserClinicRole.Role.CLINIC_ADMIN
        ).pk
    return _MANAGERS[graph.clinic_a]


@dataclass(frozen=True, slots=True)
class InboxWorld:
    """One clinic-A WhatsApp account with its own signing secret."""

    graph: RbacGraph
    account: UUID
    key: str = field(repr=False)
    principal: UUID
    phone_number_id: str = PHONE_NUMBER_ID

    def headers(self, body: bytes) -> dict[str, str]:
        return {"X-Hub-Signature-256": sign(secret=bytes.fromhex(self.key), body=body)}

    def receive(self, body: bytes) -> InboundReceipt:
        with runtime_role():
            return receive_inbound(
                provider="whatsapp",
                account_id=self.account,
                headers=self.headers(body),
                body=body,
            )

    def message_body(
        self, *, text: str, event: str | None = None, sender: str = CONTACT
    ) -> bytes:
        return whatsapp_body(
            self.phone_number_id,
            messages=[
                {
                    "from": sender,
                    "id": event or f"wamid.{uuid4().hex}",
                    "timestamp": TIMESTAMP,
                    "type": "text",
                    "text": {"body": text},
                }
            ],
        )

    def status_body(
        self, *, provider_message_id: str, status: str, category: str = "utility"
    ) -> bytes:
        return whatsapp_body(
            self.phone_number_id,
            statuses=[
                {
                    "id": provider_message_id,
                    "status": status,
                    "timestamp": TIMESTAMP,
                    "recipient_id": CONTACT,
                    "pricing": {
                        "billable": True,
                        "pricing_model": "PMP",
                        "type": "regular",
                        "category": category,
                    },
                }
            ],
        )


def whatsapp_body(
    phone_number_id: str,
    *,
    messages: Sequence[dict[str, object]] = (),
    statuses: Sequence[dict[str, object]] = (),
    extra: dict[str, object] | None = None,
) -> bytes:
    value: dict[str, object] = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "551100000000",
            "phone_number_id": phone_number_id,
        },
        "contacts": [{"profile": {"name": "Paciente Sintetico"}, "wa_id": CONTACT}],
        "messages": list(messages),
        "statuses": list(statuses),
    }
    root: dict[str, object] = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": value}]}],
    }
    root.update(extra or {})
    return json.dumps(root).encode()


def inbox_account(
    graph: RbacGraph,
    settings: SettingsWrapper,
    *,
    phone_number_id: str = PHONE_NUMBER_ID,
) -> InboxWorld:
    """Provision principal, account and secret the way the owner CLI would.

    Provider account ids are unique across tenants: a caller that provisions
    several worlds in one database passes its own ``phone_number_id``.
    """
    settings.COMMS_SYNTHETIC_CHANNELS = ("email", "sms", "whatsapp")
    key = secrets.token_hex(32)
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name=f"inbox-{uuid4().hex}",
            db_identity=f"clinic_agent_msg_{uuid4().hex}",
            purpose="messaging_ingest",
        )
        account = ProviderAccount.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            channel="whatsapp",
            provider="whatsapp-cloud-v1",
            external_account_id=phone_number_id,
            principal=principal,
            signing_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        )
    root = Path(str(settings.CLINIC_SECRET_DIR))
    path = root / f"{secret_name(account.pk)}.secret"
    path.write_text(key)
    path.chmod(0o600)
    return InboxWorld(graph, account.pk, key, principal.pk, phone_number_id)
