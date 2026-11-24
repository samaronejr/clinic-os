"""Write pre-sequence inbox histories through the *base* revision's own code.

Loaded with ``runpy`` by ``migration_upgrade_seed.py`` inside the base
worktree, so it may only use entrypoints that exist there: the staff send and
retry commands, the outbox worker and the signed inbound webhook. Every
attempt, acceptance and provider failure below is written by those paths;
the only test seam is the clock or primary key the base ORM gives one retry's
INSERT, modelling a web worker whose clock runs behind or ties.

Cases (one conversation each):

* ``skew``: A accepted, provider fails A, staff retry B with its INSERT clock
  ten seconds behind A; B accepted.
* ``tie``: as ``skew`` but B's clock equals A's and B's UUID sorts below A's.
* ``chain``: A accepted, provider fails A, staff retry B, the thread is
  closed, so the worker cancels B without sending.
* ``ambiguous``: A accepted and failed, retry B accepted and failed, retry C
  still queued: two retries the base records no order between.

Only identifiers and the account's phone-number id are exported; the
account signing key stays in ``CLINIC_SECRET_DIR``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID, uuid4

from apps.comms.inbound_providers import sign
from apps.comms.inbox_inbound import receive_inbound, secret_name
from apps.comms.inbox_services import (
    retry_message,
    send_message,
    set_conversation_state,
)
from apps.comms.models import (
    Conversation,
    IntegrationOperation,
    Message,
    ProviderAccount,
)
from apps.core.integration import execute_operation
from apps.identity.models import ServicePrincipal, User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.db import connection, transaction
from django.test import override_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.models import Model

PHONE_NUMBER_ID = "100000000000901"
TIMESTAMP = "1790000000"
SENDERS = {
    "skew": "5511999990901",
    "tie": "5511999990902",
    "chain": "5511999990903",
    "ambiguous": "5511999990904",
}


def _body(**value: object) -> bytes:
    change = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "551100000000",
            "phone_number_id": PHONE_NUMBER_ID,
        },
        "contacts": [
            {"profile": {"name": "Paciente Sintetico"}, "wa_id": "5511999990900"}
        ],
        "messages": [],
        "statuses": [],
        **value,
    }
    root = {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": "waba-upgrade", "changes": [{"field": "messages", "value": change}]}
        ],
    }
    return json.dumps(root).encode()


def status_body(reference: str, status: str, stamp: str = TIMESTAMP) -> bytes:
    """One signed-shape WhatsApp status report for one provider reference."""
    return _body(
        statuses=[
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
        ]
    )


class _Seed:
    def __init__(self, organization_id: UUID, clinic_id: UUID) -> None:
        self.organization_id = organization_id
        self.clinic_id = clinic_id
        self.manager_id = uuid4()
        self.key = secrets.token_hex(32)
        self.account_id: UUID | None = None

    @contextmanager
    def owner(self) -> Iterator[None]:
        """Owner-side reads and provisioning inside the seeded tenant."""
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(self.organization_id)],
            )
            yield

    def provision(self) -> None:
        with self.owner():
            manager = User.objects.create(
                id=self.manager_id, username=f"upgrade-inbox-{self.manager_id.hex[:8]}"
            )
            UserClinicRole.objects.create(
                user=manager,
                organization_id=self.organization_id,
                clinic_id=self.clinic_id,
                role=UserClinicRole.Role.CLINIC_ADMIN,
            )
            principal = ServicePrincipal.objects.create(
                organization_id=self.organization_id,
                clinic_id=self.clinic_id,
                name=f"inbox-{uuid4().hex}",
                db_identity=f"clinic_agent_msg_{uuid4().hex}",
                purpose="messaging_ingest",
            )
            account = ProviderAccount.objects.create(
                organization_id=self.organization_id,
                clinic_id=self.clinic_id,
                channel="whatsapp",
                provider="whatsapp-cloud-v1",
                external_account_id=PHONE_NUMBER_ID,
                principal=principal,
                signing_key_digest=hashlib.sha256(self.key.encode()).hexdigest(),
            )
        self.account_id = account.pk
        path = (
            Path(str(settings.CLINIC_SECRET_DIR)) / f"{secret_name(account.pk)}.secret"
        )
        path.write_text(self.key)
        path.chmod(0o600)

    def receive(self, body: bytes) -> None:
        if self.account_id is None:
            message = "the inbox account must be provisioned first"
            raise RuntimeError(message)
        with runtime():
            receive_inbound(
                provider="whatsapp",
                account_id=self.account_id,
                headers={
                    "X-Hub-Signature-256": sign(
                        secret=bytes.fromhex(self.key), body=body
                    )
                },
                body=body,
            )

    def staff(self, call: Callable[[], object]) -> object:
        with runtime(), tenant_context(self.manager_id, self.organization_id):
            return call()

    def conversation(self, case: str) -> UUID:
        event = f"wamid.upgrade.{case}.{uuid4().hex}"
        self.receive(
            _body(
                messages=[
                    {
                        "from": SENDERS[case],
                        "id": event,
                        "timestamp": TIMESTAMP,
                        "type": "text",
                        "text": {"body": "Oi"},
                    }
                ]
            )
        )
        with self.owner():
            return Message.objects.get(event_id=event).conversation_id

    def pending(self, message_id: UUID) -> IntegrationOperation:
        with self.owner():
            return IntegrationOperation.objects.get(
                subject_id=message_id, status="pending"
            )

    def dispatch(self, message_id: UUID) -> IntegrationOperation:
        operation = self.pending(message_id)
        with runtime():
            execute_operation(operation.pk)
        with self.owner():
            operation.refresh_from_db()
        return operation

    def fail(self, operation: IntegrationOperation) -> None:
        if not operation.provider_reference:
            message = "only an accepted attempt can receive a provider failure"
            raise RuntimeError(message)
        self.receive(status_body(operation.provider_reference, "failed"))

    def retry(self, message_id: UUID) -> None:
        self.staff(
            lambda: retry_message(
                clinic_id=self.clinic_id, message_id=message_id, command_id=uuid4()
            )
        )


@contextmanager
def runtime() -> Iterator[None]:
    """Act as the runtime role, as the web and worker processes do."""
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _with_insert_seam(
    seed: _Seed, first: IntegrationOperation, message_id: UUID, *, tie: bool
) -> None:
    """Retry with the base ORM's INSERT clock (and, for ``tie``, key) chosen."""
    meta = IntegrationOperation._meta  # noqa: SLF001 - the base ORM's INSERT seam
    created_at = meta.get_field("created_at")
    stamp = first.created_at if tie else first.created_at - timedelta(seconds=10)

    def clock(instance: Model, *, add: bool) -> object:
        del add  # Django passes it by keyword; every seam call is an INSERT.
        setattr(instance, created_at.attname, stamp)
        return stamp

    with ExitStack() as seams:
        seams.enter_context(patch.object(created_at, "pre_save", clock))
        if tie:
            lower = UUID(int=first.pk.int - 1)
            seams.enter_context(patch.object(meta.pk, "get_default", lambda: lower))
        seed.retry(message_id)


def _case(seed: _Seed, case: str) -> dict[str, str]:
    conversation_id = seed.conversation(case)
    message_id = seed.staff(
        lambda: (
            send_message(
                clinic_id=seed.clinic_id,
                conversation_id=conversation_id,
                command_id=uuid4(),
                body=f"Mensagem sintetica {case}",
            ).message_id
        )
    )
    if not isinstance(message_id, UUID):
        message = "send_message returned no message identifier"
        raise TypeError(message)
    first = seed.dispatch(message_id)
    seed.fail(first)
    ids = {
        "conversation": str(conversation_id),
        "message": str(message_id),
        "a": str(first.pk),
    }
    if case in {"skew", "tie"}:
        _with_insert_seam(seed, first, message_id, tie=case == "tie")
        ids["b"] = str(seed.dispatch(message_id).pk)
    elif case == "chain":
        seed.retry(message_id)
        successor = seed.pending(message_id)
        with seed.owner():
            revision = Conversation.objects.get(pk=conversation_id).revision
        seed.staff(
            lambda: set_conversation_state(
                clinic_id=seed.clinic_id,
                conversation_id=conversation_id,
                state="closed",
                expected_revision=revision,
            )
        )
        with runtime():
            execute_operation(successor.pk)
        ids["b"] = str(successor.pk)
    else:
        seed.retry(message_id)
        second = seed.dispatch(message_id)
        seed.fail(second)
        seed.retry(message_id)
        ids["b"] = str(second.pk)
        ids["c"] = str(seed.pending(message_id).pk)
    return ids


def seed_inbox(markers: dict[str, object]) -> dict[str, object]:
    """Write the four histories into the seeded clinic; return their identifiers."""
    seed = _Seed(UUID(str(markers["organization_id"])), UUID(str(markers["clinic_id"])))
    with override_settings(COMMS_SYNTHETIC_CHANNELS=("email", "sms", "whatsapp")):
        seed.provision()
        cases = {case: _case(seed, case) for case in SENDERS}
    return {
        "account_id": str(seed.account_id),
        "manager_id": str(seed.manager_id),
        "phone_number_id": PHONE_NUMBER_ID,
        "cases": cases,
    }
