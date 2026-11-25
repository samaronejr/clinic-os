"""Outbound inbox adapters on the shared outbox, plus the send-time recheck.

Preparation runs inside the worker's tenant transaction and rebuilds the request
from stored rows; the network call happens afterwards with no open transaction.
No live transport exists: a real send is blocked on EG-1 (provider selection and
owner approval), so ``send`` answers only an explicitly synthetic acceptance
reference for channels enabled as synthetic, or fails closed. The recheck runs
the same window, template and consent rules as the command, so a message queued
before the window closed, a template was revoked or a thread was closed is
cancelled rather than sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.db import DatabaseError, connections, transaction
from django.utils import timezone

from apps.comms.adapters import (
    AmbiguousSendError,
    PermanentSendError,
    SendResult,
    TransientSendError,
)
from apps.comms.capabilities import channel_capability
from apps.comms.inbound_providers import WebhookRejectedError, normalize_address
from apps.comms.inbox_rules import SendRequest, decide_send
from apps.comms.inbox_services import (
    PROVIDER_BY_CHANNEL,
    SUBJECT_TYPE,
    contact_fingerprints,
    conversation_lock_key,
    marketing_opted_in,
)
from apps.comms.models import IntegrationOperation, Message
from apps.comms.whatsapp_payloads import template_payload, text_payload
from apps.core.fault_injection import injected_fault
from apps.core.integration import bound_lock_waits, is_contention, retry_contention
from apps.core.secrets import SecretUnavailableError, secret_store
from apps.intake.models import PatientClinicEnrollment, PatientContact

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.comms.adapters import OperationScope
    from apps.comms.models import Conversation


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Ephemeral request, never stored or logged; the body lives only in memory."""

    channel: str
    destination: str
    body: str
    payload: dict[str, object]


def _message_for(operation: IntegrationOperation) -> Message:
    return Message.objects.select_related("conversation", "template").get(
        pk=operation.subject_id, direction="out"
    )


def _marketing_destination(conversation: Conversation) -> str | None:
    """Bind the immutable thread identity to the current verified destination."""
    if conversation.enrollment_id is None:
        return None
    enrollment = PatientClinicEnrollment.objects.filter(
        pk=conversation.enrollment_id, clinic_id=conversation.clinic_id
    ).first()
    if enrollment is None:
        return None
    contact = PatientContact.objects.filter(
        patient_id=enrollment.patient_id, channel=conversation.channel
    ).first()
    if contact is None or contact.verified_version != contact.destination_version:
        return None
    try:
        destination = normalize_address(conversation.channel, contact.destination)
    except WebhookRejectedError:
        return None
    if conversation.contact_fingerprint not in contact_fingerprints(
        channel=conversation.channel, address=destination
    ):
        return None
    return destination


class InboxAdapter:
    """Shared mechanics; each channel keeps its own capability gate and provider."""

    channel: ClassVar[str]
    provider: str

    def prepare(self, operation: IntegrationOperation) -> OutboundMessage:
        """Rebuild the request from stored rows under current tenant authority."""
        if operation.channel != self.channel or operation.subject_type != SUBJECT_TYPE:
            raise PermanentSendError
        message = _message_for(operation)
        conversation = message.conversation
        if conversation.channel != self.channel:
            raise PermanentSendError
        destination = conversation.contact_address
        template = message.template
        marketing = conversation.purpose == "marketing"
        if marketing:
            from apps.crm.rules import MARKETING_BODIES  # noqa: PLC0415

            if template is None or template.body not in MARKETING_BODIES:
                raise PermanentSendError
            current_destination = _marketing_destination(conversation)
            if current_destination is None:
                raise PermanentSendError
            destination = current_destination
        # Marketing bytes come from the immutable neutral template, never an
        # independently inserted encrypted body or arbitrary stored parameters.
        body = template.body if marketing and template is not None else message.body
        payload: dict[str, object] = {}
        if self.channel == "whatsapp":
            payload = (
                template_payload(
                    to=destination,
                    name=template.name,
                    language=template.language,
                    parameters=(
                        ()
                        if marketing
                        else tuple(json.loads(message.template_parameters or "[]"))
                    ),
                )
                if template is not None
                else text_payload(to=destination, body=body)
            )
        return OutboundMessage(self.channel, destination, body, payload)

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return only a synthetic acceptance reference, or fail closed."""
        if (
            not isinstance(prepared, OutboundMessage)
            or prepared.channel != self.channel
        ):
            raise PermanentSendError
        if self.channel == "portal":
            return SendResult(f"portal:{operation_id}")
        if not channel_capability(self.channel).synthetic_enabled:
            # Real delivery has no transport until EG-1; never pretend otherwise.
            raise PermanentSendError
        if self.channel in getattr(settings, "COMMS_SYNTHETIC_FAILURE_CHANNELS", ()):
            raise TransientSendError
        # Degraded-mode drills: an unknown outcome is held for reconciliation.
        drill = injected_fault("messaging")
        if drill == "ambiguous":
            raise AmbiguousSendError
        if drill is not None:
            raise TransientSendError
        return SendResult(f"synthetic:{self.channel}:{operation_id}")


class WhatsAppInboxAdapter(InboxAdapter):
    """Cloud API shaped requests; synthetic acceptance only."""

    channel = "whatsapp"
    provider = PROVIDER_BY_CHANNEL["whatsapp"]


class EmailInboxAdapter(InboxAdapter):
    """Email replies, separately gated from both phone channels."""

    channel = "email"
    provider = PROVIDER_BY_CHANNEL["email"]


class SMSInboxAdapter(InboxAdapter):
    """SMS replies, separately gated from email and WhatsApp."""

    channel = "sms"
    provider = PROVIDER_BY_CHANNEL["sms"]


class PortalInboxAdapter(InboxAdapter):
    """In-app delivery: nothing leaves the system, so no capability gate applies."""

    channel = "portal"
    provider = PROVIDER_BY_CHANNEL["portal"]


_SEND_CHECKS: list[Callable[[Message], bool]] = []


def register_message_send_check(check: Callable[[Message], bool]) -> None:
    """Let the domain that queued an outbound message veto it at send time.

    Each check runs inside the send's tenant transaction, under the sender's
    authority, after the channel rules; any ``False`` cancels the operation.
    """
    if check not in _SEND_CHECKS:
        _SEND_CHECKS.append(check)


def message_send_eligible(scope: OperationScope) -> bool:
    """Re-decide the send under current facts; False cancels without a side effect."""
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        return False
    message = (
        Message.objects.select_related("conversation", "template")
        .filter(pk=operation.subject_id, direction="out")
        .first()
    )
    if message is None or message.conversation.clinic_id != scope.clinic_id:
        return False
    if message.provider_state in {"delivered", "read"}:
        return False
    # Only the message's current attempt may send: never one awaiting staff
    # reconciliation, and never one a later attempt has superseded.
    attempts = list(
        IntegrationOperation.objects.filter(
            subject_type=SUBJECT_TYPE, subject_id=message.pk
        ).values_list("attempt_sequence", "send_permit")
    )
    if (
        operation.attempt_sequence is None
        or (None, None) in attempts
        or operation.attempt_sequence != max(n for n, _ in attempts if n is not None)
    ):
        return False
    conversation, template = message.conversation, message.template
    if conversation.purpose == "marketing":
        from apps.crm.rules import MARKETING_BODIES  # noqa: PLC0415

        if (
            template is None
            or template.body not in MARKETING_BODIES
            or _marketing_destination(conversation) is None
        ):
            return False
    decision = decide_send(
        SendRequest(
            channel=conversation.channel,
            purpose=conversation.purpose,
            conversation_state=conversation.state,
            now=timezone.now(),
            last_inbound_at=conversation.last_inbound_at,
            template_category=template.category if template else None,
            template_status=template.approval_status if template else None,
            template_channel=template.channel if template else None,
            linked_to_patient=conversation.enrollment_id is not None,
        )
    )
    return (
        decision.allowed
        and (
            conversation.purpose != "marketing"
            or marketing_opted_in(
                enrollment_id=conversation.enrollment_id, channel=conversation.channel
            )
        )
        and all(check(message) for check in _SEND_CHECKS)
    )


def send_permit(scope: OperationScope) -> bool:
    """Take the message's send permit; integration runs it outside any transaction.

    The claim may wait on locks (bounded by the lock session's lock_timeout
    and retried while the lock is held), so it never runs inside the tenant
    transaction of the recheck: a long stall cannot expire that transaction.
    """
    if scope.operation_id in _CLAIMS:
        return True
    row = _claim_statement(scope.operation_id)
    decision = row[0] if row else "refused"
    if decision == "claimed":
        _CLAIMS.add(scope.operation_id)
    return decision in {"claimed", "eligible"}


# In-flight claims this worker process holds. The database binds each to the
# lock session that took it; nothing here is a secret.
_CLAIMS: set[UUID] = set()


def _claim_statement(operation_id: UUID) -> tuple[object, ...] | None:
    """Run the claim, retrying its own statement on lock contention.

    Each try is one autocommit statement on the lock session (the clinic_lock
    login itself, never clinic_app), its lock waits bounded by the session's
    lock_timeout, so a contended try claimed nothing and the attempt stays
    claimable. It retries for as long as the lock session is alive and still
    holds its subject lock; otherwise it fails closed.
    """
    while True:
        try:
            with connections["locks"].cursor() as cursor:
                bound_lock_waits(cursor)
                cursor.execute(
                    "SELECT clinic_app.comms_claim_send_permit(%s)", [operation_id]
                )
                row: tuple[object, ...] | None = cursor.fetchone()
                return row
        except DatabaseError as error:
            if not retry_contention(error):
                raise


def forget_claim(operation_id: UUID) -> None:
    """Drop this process's claim once its recording is given up."""
    _CLAIMS.discard(operation_id)


def outcome_proof(
    account_id: UUID, operation_id: UUID, outcome: str, reference: str | None
) -> str:
    """Sign a synchronous provider answer with the account's provider secret.

    The secret comes from the adapter's own configuration; the database checks
    the MAC against its resolver-only copy and binds it to this attempt,
    outcome and reference.
    """
    from apps.comms.inbox_inbound import secret_name  # noqa: PLC0415

    key = bytes.fromhex(secret_store().get_secret(secret_name(account_id)))
    message = f"clinic-comms-outcome-v1:{operation_id}:{outcome}:{reference or ''}"
    return hmac.digest(key, message.encode(), hashlib.sha256).hex()


_UNSENT_CLOSE: dict[str, str] = {
    "withdrawn": "cancelled",
    "rejected": "failed",
    "retry": "pending",
}


def record_claimed_outcome(
    operation_id: UUID, outcome: str, reference: str | None, reason: str | None
) -> bool | None:
    """Record a message attempt's outcome through its resolvers.

    A claim this process holds is recorded by its own lock session with the
    provider's MAC. An outbox-claimed attempt that never took the send permit's
    claim provably never reached the provider and is closed by
    ``comms_close_unsent_attempt``. Anything else is not a message attempt
    (``None``). A refusal records nothing: the attempt stays visibly in flight.
    """
    if operation_id not in _CLAIMS:
        return _close_unsent(operation_id, outcome, reason)
    try:
        with transaction.atomic(using="locks"), connections["locks"].cursor() as cursor:
            cursor.execute(
                "SELECT account FROM clinic_app.comms_attempt_scope(%s)",
                [operation_id],
            )
            account = cursor.fetchone()
            proof = (
                outcome_proof(account[0], operation_id, outcome, reference)
                if account and account[0] and outcome in {"accepted", "rejected"}
                else None
            )
            cursor.execute(
                "SELECT clinic_app.comms_record_send_outcome(%s, %s, %s, %s)",
                [operation_id, outcome, reference, proof],
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        # A deadlock or serialization failure keeps the claim: the caller
        # retries this recording in a new transaction under the same claim.
        if is_contention(error):
            raise
        forget_claim(operation_id)
        return False
    except SecretUnavailableError:
        forget_claim(operation_id)
        return False
    # The claim is spent only once the recording transaction commits.
    transaction.on_commit(lambda: forget_claim(operation_id), using="locks")
    return bool(row and row[0])


def _close_unsent(operation_id: UUID, outcome: str, reason: str | None) -> bool | None:
    with connections["locks"].cursor() as cursor:
        cursor.execute(
            "SELECT status FROM clinic_app.comms_attempt_scope(%s)", [operation_id]
        )
        row = cursor.fetchone()
        if row is None or row[0] != "in_progress" or outcome not in _UNSENT_CLOSE:
            return None
        try:
            with transaction.atomic(using="locks"):
                cursor.execute(
                    "SELECT clinic_app.comms_close_unsent_attempt(%s, %s, %s)",
                    [
                        operation_id,
                        _UNSENT_CLOSE[outcome],
                        reason or "subject_ineligible",
                    ],
                )
                closed = cursor.fetchone()
        except DatabaseError as error:
            if is_contention(error):
                raise
            return False
    return bool(closed and closed[0])


def message_lock_key(scope: OperationScope, subject_id: UUID) -> str | None:
    """Serialize sends with thread mutations such as close and patient link."""
    thread = (
        Message.objects.filter(pk=subject_id, conversation__clinic_id=scope.clinic_id)
        .values_list("conversation_id", "conversation__purpose")
        .first()
    )
    if thread is None:
        return None
    if thread[1] == "marketing":
        # The stable clinic parent also covers a preference or purpose consent
        # changed before a new conversation exists (todo 62).
        return f"crm.marketing:{scope.clinic_id}"
    return conversation_lock_key(thread[0])
