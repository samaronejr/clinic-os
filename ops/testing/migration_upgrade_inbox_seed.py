"""Write pre-sequence inbox histories through the *base* revision's own code.

Loaded with ``runpy`` by ``migration_upgrade_seed.py`` inside the base
worktree, so it may only use entrypoints that exist there: the staff send,
retry and close commands, the outbox worker and the signed inbound webhooks.
Every attempt, acceptance and provider report below is written by those
paths; the only test seam is the clock or primary key the base ORM gives one
retry's INSERT, modelling a web worker whose clock runs behind or ties.

Each case is one conversation written by a fixed step script (see
``CASES``). After its last step the base's own ``message_states`` is read and
exported as ``base_state``, so the candidate can be held to exactly what the
base showed staff and whether the base offered a retry.

Only identifiers are exported; the account signing keys stay in
``CLINIC_SECRET_DIR``.
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

from apps.comms.adapters import AmbiguousSendError, PermanentSendError
from apps.comms.inbound_providers import sign
from apps.comms.inbox_inbound import receive_inbound, secret_name
from apps.comms.inbox_services import (
    message_states,
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
from apps.core import integration
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
ENVELOPE_ACCOUNTS = {"email": "upgrade-caixa-1", "sms": "upgrade-sms-1"}
TIMESTAMP = "1790000000"
LATE = "1790000050"

# Step vocabulary: ("send",), ("dispatch", label), ("status", label, state[,
# stamp]), ("retry"|"retry_skew"|"retry_tie",), ("close",). Labels name
# attempts in creation order: a, b, c.
_A_FAILED = (("send",), ("dispatch", "a"), ("status", "a", "failed"))
CASES: dict[str, tuple[str, tuple[tuple[str, ...], ...]]] = {
    # Single attempt: every outbox and provider outcome the base can record.
    "p_queued": ("whatsapp", (("send",),)),
    "p_sent": ("whatsapp", (("send",), ("dispatch", "a"))),
    "p_sent_receipt": (
        "whatsapp",
        (("send",), ("dispatch", "a"), ("status", "a", "sent")),
    ),
    "p_delivered": (
        "whatsapp",
        (("send",), ("dispatch", "a"), ("status", "a", "delivered")),
    ),
    "p_read": ("whatsapp", (("send",), ("dispatch", "a"), ("status", "a", "read"))),
    "p_failed": ("whatsapp", _A_FAILED),
    "p_failed_delivered": (
        "whatsapp",
        (*_A_FAILED, ("status", "a", "delivered", LATE)),
    ),
    "p_cancelled": ("whatsapp", (("send",), ("close",), ("dispatch", "a"))),
    # The original and one retry, in every outcome of the retry.
    "r_queued": ("whatsapp", (*_A_FAILED, ("retry",))),
    "r_sent": ("whatsapp", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    "r_failed": (
        "whatsapp",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "failed")),
    ),
    "r_delivered": (
        "whatsapp",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "delivered")),
    ),
    "r_cancelled": (
        "whatsapp",
        (*_A_FAILED, ("retry",), ("close",), ("dispatch", "b")),
    ),
    # Email/SMS receipts name no attempt; a sole attempt still has parity.
    "e_failed": ("email", _A_FAILED),
    "e_retry_sent": ("email", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    "e_retry_failed": (
        "email",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "failed")),
    ),
    # Round-3 worker clocks: the retry's INSERT behind, or tied and lower.
    "skew": ("whatsapp", (*_A_FAILED, ("retry_skew",), ("dispatch", "b"))),
    "tie": ("whatsapp", (*_A_FAILED, ("retry_tie",), ("dispatch", "b"))),
    # Two retries: the base recorded no order between them.
    "ambiguous": (
        "whatsapp",
        (
            *_A_FAILED,
            ("retry",),
            ("dispatch", "b"),
            ("status", "b", "failed"),
            ("retry",),
        ),
    ),
    # Three attempts the provider never accepted (the thread was closed).
    "none_accepted": (
        "whatsapp",
        (
            ("send",),
            ("close",),
            ("dispatch", "a"),
            ("retry",),
            ("dispatch", "b"),
            ("retry",),
        ),
    ),
    # SMS: the same envelope receipts as email, which name no attempt.
    "s_sent": ("sms", (("send",), ("dispatch", "a"))),
    "s_delivered": (
        "sms",
        (("send",), ("dispatch", "a"), ("status", "a", "delivered")),
    ),
    "s_failed": ("sms", _A_FAILED),
    "e_delivered": (
        "email",
        (("send",), ("dispatch", "a"), ("status", "a", "delivered")),
    ),
    "s_retry_sent": ("sms", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    "s_retry_failed": (
        "sms",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "failed")),
    ),
    # Outbox-level outcomes: rejected (terminal failed) and ambiguous (unknown).
    "o_rejected": ("whatsapp", (("send",), ("fault", "a", "rejected"))),
    "o_unknown": ("whatsapp", (("send",), ("fault", "a", "unknown"))),
    "o_rejected_retry_sent": (
        "whatsapp",
        (("send",), ("fault", "a", "rejected"), ("retry",), ("dispatch", "b")),
    ),
    "s_rejected": ("sms", (("send",), ("fault", "a", "rejected"))),
    "e_unknown": ("email", (("send",), ("fault", "a", "unknown"))),
    # Round 5: a later failure of a base-retried email/SMS message, and two
    # workers dispatching the next retry at once.
    **dict.fromkeys(
        ("e_stuck", "e_two_dispatch"),
        ("email", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    ),
    "s_stuck": ("sms", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    # Round 5 (b): B fails too, C (lowest UUID) is accepted, then A fails late.
    "r5_b": (
        "whatsapp",
        (
            *_A_FAILED,
            ("retry",),
            ("dispatch", "b"),
            ("status", "b", "failed"),
            ("retry_tie",),
            ("dispatch", "c"),
            ("status", "a", "failed", LATE),
        ),
    ),
    # Round 10: an accepted send the base recorded, rewritten afterwards.
    "r10_rewrite": ("whatsapp", (("send",), ("dispatch", "a"))),
    # Round 8: the base left a send in flight (ambiguous provider answer).
    "r8_legacy_inflight": ("whatsapp", (("send",), ("fault", "a", "unknown"))),
    # Round 7: a single queued attempt the candidate's worker sends.
    **dict.fromkeys(("r7_inflight", "r7_rejected"), ("whatsapp", (("send",),))),
    # Round 6: the base itself left two accepted attempts (B and C) open.
    **dict.fromkeys(
        ("r6_two_open", "r6_raw_sql"),
        (
            "whatsapp",
            (
                *_A_FAILED,
                ("retry",),
                ("dispatch", "b"),
                ("status", "a", "failed", LATE),
                ("retry",),
                ("dispatch", "c"),
            ),
        ),
    ),
    # Round 4: B accepted under a skewed clock, a late failure for A makes the
    # base itself show failed and queue C.
    **dict.fromkeys(
        ("accepted_then_queued", "queued_cancelled_first", "race", "r5_a", "crash"),
        (
            "whatsapp",
            (
                *_A_FAILED,
                ("retry_skew",),
                ("dispatch", "b"),
                ("status", "a", "failed", LATE),
                ("retry",),
            ),
        ),
    ),
}


def whatsapp_body(**value: object) -> bytes:
    """One WhatsApp Cloud webhook body carrying the given message/status lists."""
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


def status_body(channel: str, reference: str, status: str, stamp: str) -> bytes:
    """One provider status report for one provider reference."""
    if channel in ENVELOPE_ACCOUNTS:
        return json.dumps(
            {
                "account": ENVELOPE_ACCOUNTS[channel],
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
    return whatsapp_body(
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


def _inbound_message(channel: str, sender: str, event: str) -> bytes:
    if channel in ENVELOPE_ACCOUNTS:
        return json.dumps(
            {
                "account": ENVELOPE_ACCOUNTS[channel],
                "events": [
                    {
                        "kind": "message",
                        "event_id": event,
                        "from": sender,
                        "text": "Oi",
                        "sent_at": int(TIMESTAMP),
                    }
                ],
            }
        ).encode()
    return whatsapp_body(
        messages=[
            {
                "from": sender,
                "id": event,
                "timestamp": TIMESTAMP,
                "type": "text",
                "text": {"body": "Oi"},
            }
        ]
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


class _Seed:
    def __init__(self, organization_id: UUID, clinic_id: UUID) -> None:
        self.organization_id = organization_id
        self.clinic_id = clinic_id
        self.manager_id = uuid4()
        self.keys = {c: secrets.token_hex(32) for c in ("whatsapp", "email", "sms")}
        self.accounts: dict[str, UUID] = {}

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
            for channel, provider, external in (
                ("whatsapp", "whatsapp-cloud-v1", PHONE_NUMBER_ID),
                ("email", "inbox-email-v1", ENVELOPE_ACCOUNTS["email"]),
                ("sms", "inbox-sms-v1", ENVELOPE_ACCOUNTS["sms"]),
            ):
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
                    channel=channel,
                    provider=provider,
                    external_account_id=external,
                    principal=principal,
                    signing_key_digest=hashlib.sha256(
                        self.keys[channel].encode()
                    ).hexdigest(),
                )
                self.accounts[channel] = account.pk
        for channel, account_id in self.accounts.items():
            path = Path(str(settings.CLINIC_SECRET_DIR)) / (
                f"{secret_name(account_id)}.secret"
            )
            path.write_text(self.keys[channel])
            path.chmod(0o600)

    def receive(self, channel: str, body: bytes) -> None:
        signature = sign(secret=bytes.fromhex(self.keys[channel]), body=body)
        with runtime():
            receive_inbound(
                provider=channel,
                account_id=self.accounts[channel],
                headers={"X-Hub-Signature-256": signature},
                body=body,
            )

    def staff(self, call: Callable[[], object]) -> object:
        with runtime(), tenant_context(self.manager_id, self.organization_id):
            return call()

    def conversation(self, channel: str, index: int) -> UUID:
        sender = (
            f"paciente.upgrade.{index}@example.invalid"
            if channel == "email"
            else f"55119999{index:05d}"
        )
        event = f"upgrade.{index}.{uuid4().hex}"
        self.receive(channel, _inbound_message(channel, sender, event))
        with self.owner():
            return Message.objects.get(event_id=event).conversation_id


def _insert_seam(first: IntegrationOperation, *, tie: bool, seams: ExitStack) -> None:
    """Choose the base ORM's INSERT clock (and, for ``tie``, key) for a retry."""
    meta = IntegrationOperation._meta  # noqa: SLF001 - the base ORM's INSERT seam
    created_at = meta.get_field("created_at")
    stamp = first.created_at if tie else first.created_at - timedelta(seconds=10)

    def clock(instance: Model, *, add: bool) -> object:
        del add  # Django passes it by keyword; every seam call is an INSERT.
        setattr(instance, created_at.attname, stamp)
        return stamp

    seams.enter_context(patch.object(created_at, "pre_save", clock))
    if tie:
        lower = UUID(int=first.pk.int - 1)
        seams.enter_context(patch.object(meta.pk, "get_default", lambda: lower))


class _History:
    """One conversation's attempts, labelled in creation order."""

    def __init__(self, seed: _Seed, channel: str, index: int) -> None:
        self.seed = seed
        self.channel = channel
        self.conversation_id = seed.conversation(channel, index)
        self.message_id: UUID | None = None
        self.labels: dict[str, IntegrationOperation] = {}

    def _message(self) -> UUID:
        if self.message_id is None:
            message = "the history has not sent its message yet"
            raise RuntimeError(message)
        return self.message_id

    def _label_new_attempt(self) -> None:
        with self.seed.owner():
            known = {op.pk for op in self.labels.values()}
            fresh = [
                op
                for op in IntegrationOperation.objects.filter(
                    subject_id=self._message()
                )
                if op.pk not in known
            ]
        if len(fresh) != 1:
            message = "each send or retry enqueues exactly one attempt"
            raise RuntimeError(message)
        self.labels["abc"[len(self.labels)]] = fresh[0]

    def step(self, step: tuple[str, ...]) -> None:
        seed, verb = self.seed, step[0]
        if verb == "send":
            receipt = seed.staff(
                lambda: send_message(
                    clinic_id=seed.clinic_id,
                    conversation_id=self.conversation_id,
                    command_id=uuid4(),
                    body="Mensagem sintetica",
                )
            )
            self.message_id = getattr(receipt, "message_id", None)
            self._label_new_attempt()
        elif verb == "dispatch":
            attempt = self.labels[step[1]]
            with runtime():
                execute_operation(attempt.pk)
            with seed.owner():
                attempt.refresh_from_db()
        elif verb == "fault":
            # Outbox-level outcome: the provider rejects, or answers ambiguously.
            attempt = self.labels[step[1]]
            error = {
                "rejected": PermanentSendError,
                "unknown": AmbiguousSendError,
            }[step[2]]

            def refuse(*_args: object, **_kwargs: object) -> object:
                raise error

            adapter = integration._SEND_ADAPTERS[attempt.provider]  # noqa: SLF001
            with runtime(), patch.object(adapter, "send", refuse):
                execute_operation(attempt.pk)
            with seed.owner():
                attempt.refresh_from_db()
        elif verb == "status":
            reference = self.labels[step[1]].provider_reference
            if not reference:
                message = "only an accepted attempt can receive a provider report"
                raise RuntimeError(message)
            stamp = step[3] if len(step) > 3 else TIMESTAMP  # noqa: PLR2004
            seed.receive(
                self.channel, status_body(self.channel, reference, step[2], stamp)
            )
        elif verb == "close":
            with seed.owner():
                revision = Conversation.objects.get(pk=self.conversation_id).revision
            seed.staff(
                lambda: set_conversation_state(
                    clinic_id=seed.clinic_id,
                    conversation_id=self.conversation_id,
                    state="closed",
                    expected_revision=revision,
                )
            )
        else:
            with ExitStack() as seams:
                if verb in {"retry_skew", "retry_tie"}:
                    _insert_seam(self.labels["a"], tie=verb == "retry_tie", seams=seams)
                seed.staff(
                    lambda: retry_message(
                        clinic_id=seed.clinic_id,
                        message_id=self._message(),
                        command_id=uuid4(),
                    )
                )
            self._label_new_attempt()

    def exported(self) -> dict[str, str]:
        message_id = self._message()

        def base_state() -> str:
            message = Message.objects.get(pk=message_id)
            return str(message_states([message])[message_id])

        return {
            "channel": self.channel,
            "conversation": str(self.conversation_id),
            "message": str(message_id),
            "base_state": str(self.seed.staff(base_state)),
            **{label: str(op.pk) for label, op in self.labels.items()},
        }


def seed_inbox(markers: dict[str, object]) -> dict[str, object]:
    """Write every case into the seeded clinic; return its identifiers."""
    seed = _Seed(UUID(str(markers["organization_id"])), UUID(str(markers["clinic_id"])))
    cases: dict[str, dict[str, str]] = {}
    with override_settings(COMMS_SYNTHETIC_CHANNELS=("email", "sms", "whatsapp")):
        seed.provision()
        for index, (name, (channel, steps)) in enumerate(CASES.items(), start=1):
            history = _History(seed, channel, index)
            for step in steps:
                history.step(step)
            cases[name] = history.exported()
    return {
        "accounts": {channel: str(pk) for channel, pk in seed.accounts.items()},
        "manager_id": str(seed.manager_id),
        "cases": cases,
    }
