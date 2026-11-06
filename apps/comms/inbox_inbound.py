"""Inbound provider webhooks: authenticate raw bytes, resolve stored scope, apply.

Order is the contract. The HMAC over the exact request bytes is checked before
any JSON is parsed, before any account row is read and before any tenant exists.
Only then does the stored provider account resolve the organization, clinic and
service principal; a clinic or account named inside the payload is never read,
and events that claim another provider identifier are ignored rather than
routed. Each event id is recorded once, so a replayed delivery is acknowledged
without a second message, status change or meter row.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.comms.capabilities import channel_capability
from apps.comms.inbound_providers import (
    MAX_BODY_BYTES,
    InboundMessage,
    StatusUpdate,
    TemplateStatus,
    WebhookRejectedError,
    parse_signed_envelope,
    parse_whatsapp,
    verify_signature,
)
from apps.comms.inbox_rules import advance_provider_state
from apps.comms.inbox_services import (
    PROVIDER_BY_CHANNEL,
    SUBJECT_TYPE,
    apply_template_status,
    contact_fingerprints,
    publish_messages_hint,
)
from apps.comms.models import (
    Conversation,
    InboundEvent,
    IntegrationOperation,
    Message,
    MessageMeter,
    TemplateVersion,
)
from apps.comms.triage import classify_inbound
from apps.core.secrets import SecretUnavailableError, secret_store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from datetime import datetime
    from uuid import UUID

    from apps.comms.inbound_providers import InboundEvent as ParsedEvent

SIGNATURE_HEADERS: Final = ("X-Hub-Signature-256", "X-Inbox-Signature")
_SEVERITY: Final = {
    "": 0,
    "unclassified": 1,
    "administrative": 1,
    "clinical": 2,
    "urgent": 3,
}


class InboundRejectedError(Exception):
    """One non-identifying refusal: forged, unknown, revoked or disabled."""


@dataclass(frozen=True, slots=True)
class ResolvedAccount:
    """The stored scope of one authenticated provider account."""

    account_id: UUID
    organization_id: UUID
    clinic_id: UUID
    principal_id: UUID
    channel: str
    provider: str
    external_account_id: str


@dataclass(frozen=True, slots=True)
class InboundReceipt:
    """Counts only; the response never echoes content or identifiers."""

    accepted: int
    duplicates: int
    ignored: int
    retry: int = 0


def secret_name(account_id: UUID) -> str:
    """Name the per-account signing secret in the configured secret store."""
    return f"comms-inbox-{account_id.hex}"


def verify_token_name(account_id: UUID) -> str:
    """Name the per-account subscription verify token."""
    return f"comms-inbox-verify-{account_id.hex}"


def authenticate(*, account_id: UUID, headers: Mapping[str, str], body: bytes) -> str:
    """Verify the signature over the raw bytes; return the proof for the resolver."""
    if len(body) > MAX_BODY_BYTES:
        raise InboundRejectedError
    try:
        key = secret_store().get_secret(secret_name(account_id))
        header = next((headers[h] for h in SIGNATURE_HEADERS if h in headers), "")
        verify_signature(secret=bytes.fromhex(key), body=body, header=header)
    except (SecretUnavailableError, WebhookRejectedError, ValueError) as error:
        raise InboundRejectedError from error
    return key


def _channel_enabled(channel: str) -> bool:
    """Synthetic channels run only in synthetic mode; real ones need the registry."""
    synthetic = (
        settings.CLINIC_DATA_MODE == "synthetic"
        and channel in settings.COMMS_SYNTHETIC_CHANNELS
    )
    return synthetic or channel_capability(channel).real_enabled


@contextmanager
def inbound_context(*, account_id: UUID, proof: str) -> Iterator[ResolvedAccount]:
    """Resolve stored scope in one durable transaction; never borrow a staff actor."""
    if connection.in_atomic_block:
        raise InboundRejectedError
    with transaction.atomic(durable=True), connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.comms_resolve_inbound(%s, %s)",
            [account_id, proof],
        )
        row = cursor.fetchone()
        if row is None:
            raise InboundRejectedError
        account = ResolvedAccount(account_id, *row)
        if not _channel_enabled(account.channel):
            raise InboundRejectedError
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_principal', %s, true), "
            "set_config('app.comms_inbound_proof', %s, true)",
            [str(account.organization_id), str(account.principal_id), proof],
        )
        yield account


_MESSAGE_HOOKS: list[Callable[[UUID, str], None]] = []
_RUNNERS: list[Callable[[UUID, str], None]] = []


def register_inbound_message_hook(hook: Callable[[UUID, str], None]) -> None:
    """Run ``hook(message_id, channel)`` in the inbound transaction after a message.

    The hook shares the authenticated principal context and the event's
    savepoint: if it fails, the message and its event receipt roll back.
    """
    if hook not in _MESSAGE_HOOKS:
        _MESSAGE_HOOKS.append(hook)


def register_inbound_runner(runner: Callable[[UUID, str], None]) -> None:
    """Run ``runner(account_id, proof)`` after the delivery committed.

    The runner may re-enter ``inbound_context`` with the same proof; it runs
    outside any transaction, so it can also open a patient session.
    """
    if runner not in _RUNNERS:
        _RUNNERS.append(runner)


def receive_inbound(
    *, provider: str, account_id: UUID, headers: Mapping[str, str], body: bytes
) -> InboundReceipt:
    """Apply one authenticated delivery; replays and foreign claims change nothing."""
    proof = authenticate(account_id=account_id, headers=headers, body=body)
    with inbound_context(account_id=account_id, proof=proof) as account:
        if provider != account.channel:
            raise InboundRejectedError
        events = (
            parse_whatsapp(body)
            if account.provider == "whatsapp-cloud-v1"
            else parse_signed_envelope(account.channel, body)
        )
        receipt = _apply(account, events)
    if receipt.accepted:
        for runner in _RUNNERS:
            runner(account_id, proof)
    return receipt


class _NotAppliedError(Exception):
    """The event cannot be applied; its receipt rolls back so nothing is consumed."""


class _RetryLaterError(_NotAppliedError):
    """A status raced the send's own commit; ask the provider to redeliver."""


class _DuplicateEventError(Exception):
    """The provider event id was already recorded: a true replay, nothing else."""


def _record_event(account: ResolvedAccount, event: ParsedEvent) -> None:
    """Record the event id; only a clash on that unique key is a replay."""
    try:
        with transaction.atomic():
            InboundEvent.objects.create(
                organization_id=account.organization_id,
                provider_account_id=account.account_id,
                event_id=event.event_id,
                kind=_kind(event),
            )
    except IntegrityError as error:
        raise _DuplicateEventError from error


def _dispatch(account: ResolvedAccount, event: ParsedEvent) -> None:
    if isinstance(event, InboundMessage):
        _apply_message(account, event)
        return
    applied = (
        _apply_status(account, event)
        if isinstance(event, StatusUpdate)
        else _apply_template(account, event)
    )
    if not applied:
        raise _NotAppliedError


def _apply(account: ResolvedAccount, events: tuple[ParsedEvent, ...]) -> InboundReceipt:
    accepted = duplicates = ignored = retry = 0
    for event in events:
        if not isinstance(event, TemplateStatus) and (
            event.account_ref != account.external_account_id
        ):
            ignored += 1
            continue
        try:
            # The receipt and its effect commit or roll back together; any other
            # integrity failure propagates so the provider redelivers.
            with transaction.atomic():
                _record_event(account, event)
                _dispatch(account, event)
        except _DuplicateEventError:
            duplicates += 1
        except _RetryLaterError:
            retry += 1
        except _NotAppliedError:
            ignored += 1
        else:
            accepted += 1
    return InboundReceipt(accepted, duplicates, ignored, retry)


def _kind(event: ParsedEvent) -> str:
    if isinstance(event, InboundMessage):
        return "message"
    return "status" if isinstance(event, StatusUpdate) else "template"


def _live_conversation(
    account: ResolvedAccount, tokens: tuple[str, ...]
) -> Conversation | None:
    return (
        Conversation.objects.select_for_update()
        .filter(
            clinic_id=account.clinic_id,
            channel=account.channel,
            contact_fingerprint__in=tokens,
            purpose__in=("care", "transactional"),
        )
        .exclude(state="closed")
        .order_by("purpose")
        .first()
    )


def _open_thread(
    account: ResolvedAccount,
    tokens: tuple[str, ...],
    event: InboundMessage,
    now: datetime,
) -> Conversation:
    """Open the thread, or join the one a concurrent first message just opened."""
    try:
        with transaction.atomic():
            return Conversation.objects.create(
                organization_id=account.organization_id,
                clinic_id=account.clinic_id,
                provider_account_id=account.account_id,
                channel=account.channel,
                purpose="transactional",
                contact_fingerprint=tokens[-1],
                contact_address=event.address,
                contact_label=event.profile_name or None,
                last_activity_at=now,
            )
    except IntegrityError:
        # The live-thread key clashed: another delivery committed first. This is
        # not a replay, so re-read the thread and keep this message.
        existing = _live_conversation(account, tokens)
        if existing is None:
            raise
        return existing


def _apply_message(account: ResolvedAccount, event: InboundMessage) -> None:
    now = timezone.now()
    tokens = contact_fingerprints(channel=account.channel, address=event.address)
    conversation = _live_conversation(account, tokens)
    if conversation is None:
        conversation = _open_thread(account, tokens, event, now)
    verdict = classify_inbound(event.text) if event.supported else None
    message = Message.objects.create(
        organization_id=account.organization_id,
        conversation=conversation,
        direction="in",
        body=event.text,
        event_id=event.event_id,
        supported=event.supported,
        created_at=now,
    )
    category = verdict.category if verdict else "unclassified"
    was_flagged = conversation.needs_clinician
    if _SEVERITY[category] > _SEVERITY[conversation.triage_category]:
        conversation.triage_category = category
    conversation.needs_clinician = conversation.needs_clinician or bool(
        verdict and verdict.needs_clinician
    )
    if conversation.state == "pending":
        conversation.state = "open"
    conversation.last_activity_at = now
    conversation.revision += 1
    conversation.save(
        update_fields=[
            "triage_category",
            "needs_clinician",
            "state",
            "last_activity_at",
            "revision",
            "updated_at",
        ]
    )
    record_phase1_event(
        "comms.inbound.message_received",
        clinic_id=account.clinic_id,
        affected_record_id=message.pk,
    )
    if conversation.needs_clinician and not was_flagged:
        record_phase1_event(
            "comms.inbound.escalated",
            clinic_id=account.clinic_id,
            affected_record_id=conversation.pk,
        )
    publish_messages_hint(account.clinic_id)
    if conversation.needs_clinician:
        from apps.workflows.inbox import publish_inbox_hint  # noqa: PLC0415

        publish_inbox_hint(account.clinic_id)
    if event.supported:
        for hook in _MESSAGE_HOOKS:
            hook(message.pk, account.channel)


def _apply_status(account: ResolvedAccount, event: StatusUpdate) -> bool:
    operation = IntegrationOperation.objects.filter(
        clinic_id=account.clinic_id,
        provider=PROVIDER_BY_CHANNEL[account.channel],
        provider_reference=event.provider_message_id,
        subject_type=SUBJECT_TYPE,
    ).first()
    if operation is None:
        if IntegrationOperation.objects.filter(
            clinic_id=account.clinic_id,
            provider=PROVIDER_BY_CHANNEL[account.channel],
            subject_type=SUBJECT_TYPE,
            status__in=("pending", "in_progress"),
        ).exists():
            raise _RetryLaterError
        return False
    message = (
        Message.objects.select_for_update()
        .filter(
            pk=operation.subject_id,
            direction="out",
            conversation__clinic_id=account.clinic_id,
        )
        .first()
    )
    if message is None:
        return False
    state = advance_provider_state(
        current=message.provider_state, incoming=event.status
    )
    if state != message.provider_state:
        Message.objects.filter(pk=message.pk).update(provider_state=state)
    if event.pricing_category and event.status in {"sent", "delivered"}:
        MessageMeter.objects.get_or_create(
            organization_id=account.organization_id,
            message=message,
            source="provider",
            defaults={
                "category": event.pricing_category,
                "billable": event.billable,
                "pricing_type": event.pricing_type,
            },
        )
    record_phase1_event(
        "comms.inbound.status_applied",
        clinic_id=account.clinic_id,
        affected_record_id=message.pk,
    )
    publish_messages_hint(account.clinic_id)
    return True


def _apply_template(account: ResolvedAccount, event: TemplateStatus) -> bool:
    template: TemplateVersion | None = (
        TemplateVersion.objects.select_for_update()
        .filter(
            clinic_id=account.clinic_id,
            channel=account.channel,
            name=event.template_name,
            language=event.language,
        )
        .order_by("-version")
        .first()
    )
    if template is None or not apply_template_status(template, event.status):
        return False
    record_phase1_event(
        "comms.inbound.template_status_applied",
        clinic_id=account.clinic_id,
        affected_record_id=template.pk,
    )
    return True
