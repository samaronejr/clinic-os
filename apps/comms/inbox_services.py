"""Staff inbox commands: threads, replies, templates, notes and escalation.

Services take explicit clinic scope and never an actor; the signed-in user comes
from the transaction GUC. Every command rechecks the actor's exact clinic role
for the thread's purpose (and, for care threads, patient-scoped clinical
authority) on each call. Sending is a two-step boundary: a queued ``Message`` and
its outbox operation commit together, and the provider is called only by the
worker after commit. Nothing here sends an AI draft or any text without a
human command carrying its own idempotency key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.comms.inbound_providers import WebhookRejectedError, normalize_address
from apps.comms.inbox_rules import (
    MAX_BODY_CHARS,
    SendRequest,
    decide_send,
    effective_delivery_state,
    placeholder_count,
    render_template,
)
from apps.comms.models import (
    Conversation,
    IntegrationOperation,
    InternalNote,
    Message,
    MessageMeter,
    Participant,
    ProviderAccount,
    QuickReply,
    TemplateVersion,
)
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    hold_subject_mutation_key,
)
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
    require_permission,
)
from apps.identity.models import UserClinicRole
from apps.intake.models import PatientClinicEnrollment, PatientContact
from apps.realtime.transport import publish_on_commit
from apps.tenancy.envelope import blind_indexes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

SUBJECT_TYPE: Final = "comms.message"
PROVIDER_BY_CHANNEL: Final = {
    "whatsapp": "inbox-whatsapp-v1",
    "email": "inbox-email-v1",
    "sms": "inbox-sms-v1",
    "portal": "inbox-portal-v1",
}
_R = UserClinicRole.Role
MANAGER_ROLES: Final = (_R.CLINIC_ADMIN, _R.CLINIC_MANAGER)
CLINICIAN_ROLES: Final = (_R.NURSE, _R.PHYSICIAN)
ROLES_BY_PURPOSE: Final = {
    "transactional": (
        *MANAGER_ROLES,
        _R.RECEPTIONIST,
        _R.SCHEDULER,
        *CLINICIAN_ROLES,
    ),
    "care": CLINICIAN_ROLES,
    "marketing": MANAGER_ROLES,
}
MAX_NOTE_CHARS: Final = 4000
MAX_TEMPLATE_BODY_CHARS: Final = 1024
_MARKETING_OPT_IN_CHECKS: list[Callable[[UUID, str], bool]] = []


class Reason(StrEnum):
    """Fixed, content-free refusal and conflict codes shown to staff."""

    CHANNEL_PURPOSE_MISMATCH = "channel_purpose_mismatch"
    CONVERSATION_CLOSED = "conversation_closed"
    PATIENT_NOT_VERIFIED = "patient_not_verified"
    MARKETING_REQUIRES_TEMPLATE = "marketing_requires_template"
    TEMPLATE_NOT_APPROVED = "template_not_approved"
    TEMPLATE_CHANNEL_MISMATCH = "template_channel_mismatch"
    TEMPLATE_PURPOSE_MISMATCH = "template_purpose_mismatch"
    WINDOW_EXPIRED_TEMPLATE_REQUIRED = "window_expired_template_required"
    AI_UNAVAILABLE = "ai_unavailable"
    AI_NOT_ALLOWED = "ai_not_allowed"
    AI_DROPPED = "ai_dropped"
    ALREADY_LINKED = "already_linked"
    CHANNEL_NOT_CONFIGURED = "channel_not_configured"
    CONTACT_MISMATCH = "contact_mismatch"
    CONTACT_UNVERIFIED = "contact_unverified"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_BODY = "invalid_body"
    INVALID_SCOPE = "invalid_scope"
    INVALID_STATE = "invalid_state"
    INVALID_TEMPLATE = "invalid_template"
    LINK_NOT_APPLICABLE = "link_not_applicable"
    MARKETING_OPT_IN_MISSING = "marketing_opt_in_missing"
    NOT_FAILED = "not_failed"
    PROVIDER_APPROVAL_REQUIRED = "provider_approval_required"
    STALE_REVISION = "stale_revision"


class InboxAccessDeniedError(CurrentActorError):
    """One payload-free denial for unknown, foreign and unauthorized records."""

    def __init__(self) -> None:
        """Do not reflect identifiers or distinguish unknown from foreign."""
        super().__init__("current actor unauthorized")


class InboxConflictError(Exception):
    """A stale revision or a replay that changed its terms; the code is fixed."""

    def __init__(self, code: str) -> None:
        """Carry only a fixed, content-free code."""
        super().__init__(code)
        self.code = code


class InboxRefusedError(Exception):
    """A rule refused the command; ``code`` is one fixed reason, never content."""

    def __init__(self, code: str) -> None:
        """Carry only a fixed, content-free code."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """What a queued send returns; ``replayed`` marks an idempotent repeat."""

    message_id: UUID
    conversation_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class OpenedConversation:
    """A staff-initiated thread, or the live one that already existed."""

    conversation_id: UUID
    created: bool


def register_marketing_opt_in(check: Callable[[UUID, str], bool]) -> None:
    """Register a consent source; no source means no marketing can be sent.

    ``check(enrollment_id, channel)`` must be true only for a current,
    purpose-specific marketing opt-in on that channel. The existing contact
    preferences do not model marketing, so the default is refusal.
    """
    if check not in _MARKETING_OPT_IN_CHECKS:
        _MARKETING_OPT_IN_CHECKS.append(check)


def marketing_opted_in(*, enrollment_id: UUID | None, channel: str) -> bool:
    """Return whether any registered consent source allows marketing now."""
    return enrollment_id is not None and any(
        check(enrollment_id, channel) for check in _MARKETING_OPT_IN_CHECKS
    )


def _actor(clinic_id: UUID, roles: tuple[UserClinicRole.Role, ...]) -> UUID:
    try:
        return require_current_actor_clinic_roles(clinic_id, roles)
    except CurrentActorError as error:
        raise InboxAccessDeniedError from error


def conversation_lock_key(conversation_id: UUID) -> str:
    """Stable mutation-boundary key shared by commands and the send recheck."""
    return f"comms.conversation:{conversation_id}"


def _conversation(
    clinic_id: UUID, conversation_id: UUID, *, lock: bool = False
) -> tuple[Conversation, UUID]:
    """Load a thread the actor may handle; unknown and foreign look identical."""
    if type(clinic_id) is not UUID or type(conversation_id) is not UUID:
        raise InboxAccessDeniedError
    queryset = Conversation.objects.filter(pk=conversation_id, clinic_id=clinic_id)
    conversation = (queryset.select_for_update() if lock else queryset).first()
    if conversation is None:
        raise InboxAccessDeniedError
    actor = _actor(clinic_id, ROLES_BY_PURPOSE[conversation.purpose])
    if conversation.purpose == "care":
        try:
            require_permission(
                "clinical.read",
                clinic_id=clinic_id,
                patient_enrollment_id=conversation.enrollment_id,
            )
        except CurrentActorError as error:
            raise InboxAccessDeniedError from error
    return conversation, actor


def _revise(
    conversation: Conversation, expected_revision: int, **fields: object
) -> None:
    if conversation.revision != expected_revision:
        raise InboxConflictError(Reason.STALE_REVISION)
    for name, value in fields.items():
        setattr(conversation, name, value)
    conversation.revision += 1
    conversation.last_activity_at = timezone.now()
    conversation.save(
        update_fields=[*fields, "revision", "last_activity_at", "updated_at"]
    )


def publish_messages_hint(clinic_id: UUID) -> None:
    """Ask open inboxes to refetch after commit; the hint names no record."""
    if settings.REALTIME_ENABLED:
        publish_on_commit(
            topic=f"clinic:{clinic_id}:messages", kind="messages", version=1
        )


CONTACT_PURPOSE: Final = "comms.conversation.contact"


def contact_fingerprints(*, channel: str, address: str) -> tuple[str, ...]:
    """Tenant blind-index tokens for a contact, oldest key version first.

    The HMAC key is the tenant's data key, so tokens survive application
    secret rotation and a DEK rotation only adds a newer token. Writes store the
    last token; lookups match every token so existing threads stay reachable.
    """
    plaintext = f"{channel}\x00{address}".encode()
    return tuple(
        digest.hex()
        for digest in blind_indexes(purpose=CONTACT_PURPOSE, plaintext=plaintext)
    )


def _verified_destination(
    *, clinic_id: UUID, enrollment: PatientClinicEnrollment, channel: str
) -> tuple[ProviderAccount | None, str]:
    """Return the provider account and canonical address of a verified contact."""
    if channel == "portal":
        return None, str(enrollment.pk)
    account = (
        ProviderAccount.objects.filter(
            clinic_id=clinic_id, channel=channel, active=True
        )
        .order_by("created_at", "id")
        .first()
    )
    contact = PatientContact.objects.filter(
        patient_id=enrollment.patient_id, channel=channel
    ).first()
    if account is None:
        raise InboxRefusedError(Reason.CHANNEL_NOT_CONFIGURED)
    if contact is None or contact.verified_version != contact.destination_version:
        raise InboxRefusedError(Reason.CONTACT_UNVERIFIED)
    try:
        return account, normalize_address(channel, contact.destination)
    except WebhookRejectedError as error:
        raise InboxRefusedError(Reason.CONTACT_UNVERIFIED) from error


def open_conversation(
    *, clinic_id: UUID, enrollment_id: UUID, channel: str, purpose: str
) -> OpenedConversation:
    """Start (or return) a staff-initiated thread to a verified patient contact."""
    if channel not in {"whatsapp", "email", "sms", "portal"} or purpose not in (
        ROLES_BY_PURPOSE
    ):
        raise InboxRefusedError(Reason.INVALID_SCOPE)
    _actor(clinic_id, ROLES_BY_PURPOSE[purpose])
    enrollment = PatientClinicEnrollment.objects.filter(
        pk=enrollment_id, clinic_id=clinic_id
    ).first()
    if enrollment is None:
        raise InboxAccessDeniedError
    if purpose == "care":
        try:
            require_permission(
                "clinical.read",
                clinic_id=clinic_id,
                patient_enrollment_id=enrollment_id,
            )
        except CurrentActorError as error:
            raise InboxAccessDeniedError from error
    if purpose == "marketing" and not marketing_opted_in(
        enrollment_id=enrollment_id, channel=channel
    ):
        raise InboxRefusedError(Reason.MARKETING_OPT_IN_MISSING)
    account, address = _verified_destination(
        clinic_id=clinic_id, enrollment=enrollment, channel=channel
    )
    tokens = contact_fingerprints(channel=channel, address=address)
    live = Conversation.objects.filter(
        clinic_id=clinic_id,
        channel=channel,
        contact_fingerprint__in=tokens,
        purpose=purpose,
    ).exclude(state="closed")
    existing = live.first()
    if existing is not None:
        return OpenedConversation(existing.pk, created=False)
    now = timezone.now()
    with transaction.atomic():
        conversation = Conversation.objects.create(
            organization_id=enrollment.organization_id,
            clinic_id=clinic_id,
            provider_account=account,
            channel=channel,
            purpose=purpose,
            enrollment=enrollment,
            contact_fingerprint=tokens[-1],
            contact_address=address,
            last_activity_at=now,
        )
        Participant.objects.create(
            organization_id=enrollment.organization_id,
            conversation=conversation,
            enrollment=enrollment,
            role="patient",
        )
        record_phase1_event(
            "comms.conversation.opened",
            clinic_id=clinic_id,
            affected_record_id=conversation.pk,
        )
    publish_messages_hint(clinic_id)
    return OpenedConversation(conversation.pk, created=True)


def link_conversation_to_patient(
    *,
    clinic_id: UUID,
    conversation_id: UUID,
    enrollment_id: UUID,
    expected_revision: int,
    promote_to_care: bool = False,
) -> None:
    """Bind an inbound number to a patient only through their verified contact.

    The address on the thread must equal the patient's contact whose current
    destination version was verified; a staff member's belief is not evidence.
    Promotion to a care thread additionally needs clinician authority.
    """
    conversation, _ = _conversation(clinic_id, conversation_id, lock=True)
    hold_subject_mutation_key(conversation_lock_key(conversation_id))
    if conversation.purpose == "marketing" or conversation.channel == "portal":
        raise InboxRefusedError(Reason.LINK_NOT_APPLICABLE)
    if conversation.enrollment_id is not None:
        raise InboxRefusedError(Reason.ALREADY_LINKED)
    enrollment = PatientClinicEnrollment.objects.filter(
        pk=enrollment_id, clinic_id=clinic_id
    ).first()
    if enrollment is None:
        raise InboxAccessDeniedError
    contact = PatientContact.objects.filter(
        patient_id=enrollment.patient_id, channel=conversation.channel
    ).first()
    if contact is None or contact.verified_version != contact.destination_version:
        raise InboxRefusedError(Reason.CONTACT_UNVERIFIED)
    try:
        verified = normalize_address(conversation.channel, contact.destination)
    except WebhookRejectedError as error:
        raise InboxRefusedError(Reason.CONTACT_UNVERIFIED) from error
    if conversation.contact_fingerprint not in contact_fingerprints(
        channel=conversation.channel, address=verified
    ):
        raise InboxRefusedError(Reason.CONTACT_MISMATCH)
    fields: dict[str, object] = {"enrollment_id": enrollment_id}
    if promote_to_care:
        _actor(clinic_id, CLINICIAN_ROLES)
        try:
            require_permission(
                "clinical.read",
                clinic_id=clinic_id,
                patient_enrollment_id=enrollment_id,
            )
        except CurrentActorError as error:
            raise InboxAccessDeniedError from error
        fields["purpose"] = "care"
    with transaction.atomic():
        _revise(conversation, expected_revision, **fields)
        Participant.objects.get_or_create(
            organization_id=conversation.organization_id,
            conversation=conversation,
            enrollment_id=enrollment_id,
            role="patient",
        )
        record_phase1_event(
            "comms.conversation.patient_linked",
            clinic_id=clinic_id,
            affected_record_id=conversation.pk,
        )
    publish_messages_hint(clinic_id)


def assign_conversation(
    *,
    clinic_id: UUID,
    conversation_id: UUID,
    assignee_id: UUID | None,
    expected_revision: int,
) -> None:
    """Assign (or unassign) a thread to staff who may handle its purpose."""
    conversation, _ = _conversation(clinic_id, conversation_id, lock=True)
    if (
        assignee_id is not None
        and not UserClinicRole.objects.filter(
            user_id=assignee_id,
            clinic_id=clinic_id,
            role__in=ROLES_BY_PURPOSE[conversation.purpose],
        ).exists()
    ):
        raise InboxAccessDeniedError
    with transaction.atomic():
        _revise(
            conversation,
            expected_revision,
            assignee_id=assignee_id,
            state="open" if conversation.state == "pending" else conversation.state,
        )
        if assignee_id is not None:
            Participant.objects.get_or_create(
                organization_id=conversation.organization_id,
                conversation=conversation,
                user_id=assignee_id,
                defaults={"role": "watcher"},
            )
        record_phase1_event(
            "comms.conversation.assigned",
            clinic_id=clinic_id,
            affected_record_id=conversation.pk,
        )
    publish_messages_hint(clinic_id)


def set_conversation_state(
    *, clinic_id: UUID, conversation_id: UUID, state: str, expected_revision: int
) -> None:
    """Move a thread between open, pending and closed; closing blocks sends."""
    if state not in {"open", "pending", "closed"}:
        raise InboxRefusedError(Reason.INVALID_STATE)
    conversation, _ = _conversation(clinic_id, conversation_id, lock=True)
    hold_subject_mutation_key(conversation_lock_key(conversation_id))
    with transaction.atomic():
        _revise(conversation, expected_revision, state=state)
        record_phase1_event(
            "comms.conversation.state_changed",
            clinic_id=clinic_id,
            affected_record_id=conversation.pk,
        )
    publish_messages_hint(clinic_id)


def clear_clinician_flag(
    *, clinic_id: UUID, conversation_id: UUID, expected_revision: int
) -> None:
    """Take the clinical question; only a clinician may clear the escalation."""
    conversation, _ = _conversation(clinic_id, conversation_id, lock=True)
    _actor(clinic_id, CLINICIAN_ROLES)
    with transaction.atomic():
        _revise(conversation, expected_revision, needs_clinician=False)
        record_phase1_event(
            "comms.conversation.clinician_cleared",
            clinic_id=clinic_id,
            affected_record_id=conversation.pk,
        )
    publish_messages_hint(clinic_id)


def add_internal_note(*, clinic_id: UUID, conversation_id: UUID, body: str) -> UUID:
    """Append a staff-only note; it is never part of anything sent."""
    conversation, actor = _conversation(clinic_id, conversation_id)
    text = body.strip() if type(body) is str else ""
    if not text or len(text) > MAX_NOTE_CHARS:
        raise InboxRefusedError(Reason.INVALID_BODY)
    note = InternalNote.objects.create(
        organization_id=conversation.organization_id,
        conversation=conversation,
        author_id=actor,
        body=text,
    )
    publish_messages_hint(clinic_id)
    return note.pk


def _terms_match(
    existing: Message,
    *,
    conversation_id: UUID,
    template_version_id: UUID | None,
    rendered: str,
) -> bool:
    return (
        existing.conversation_id == conversation_id
        and existing.template_id == template_version_id
        and existing.body == rendered
    )


def _terms(
    *,
    clinic_id: UUID,
    template_version_id: UUID | None,
    body: str | None,
    parameters: tuple[str, ...],
) -> tuple[TemplateVersion | None, str]:
    """Resolve the template and the exact text this command would send."""
    template: TemplateVersion | None = None
    if template_version_id is not None:
        template = TemplateVersion.objects.filter(
            pk=template_version_id, clinic_id=clinic_id
        ).first()
        if template is None:
            raise InboxAccessDeniedError
        rendered = render_template(body=template.body, parameters=parameters)
    else:
        rendered = body.strip() if type(body) is str else None
        if rendered is not None and not 0 < len(rendered) <= MAX_BODY_CHARS:
            rendered = None
    if rendered is None:
        raise InboxRefusedError(Reason.INVALID_BODY)
    return template, rendered


def send_message(  # noqa: PLR0913 - the pinned public command signature
    *,
    clinic_id: UUID,
    conversation_id: UUID,
    command_id: UUID,
    template_version_id: UUID | None = None,
    body: str | None = None,
    parameters: tuple[str, ...] = (),
) -> SendReceipt:
    """Queue one reply: free text inside the window, otherwise an approved template.

    ``command_id`` is the idempotency key for the message and its outbox row, so
    a double click or a retry after a timeout returns the original receipt and a
    changed payload under the same key is a conflict. The provider is called
    later, by the worker, after this transaction commits.
    """
    if type(command_id) is not UUID or (template_version_id is None) == (body is None):
        raise InboxRefusedError(Reason.INVALID_BODY)
    conversation, actor = _conversation(clinic_id, conversation_id, lock=True)
    template, rendered = _terms(
        clinic_id=clinic_id,
        template_version_id=template_version_id,
        body=body,
        parameters=parameters,
    )
    existing = Message.objects.filter(
        organization_id=conversation.organization_id, command_id=command_id
    ).first()
    if existing is not None:
        if not _terms_match(
            existing,
            conversation_id=conversation_id,
            template_version_id=template_version_id,
            rendered=rendered,
        ):
            raise InboxConflictError(Reason.IDEMPOTENCY_CONFLICT)
        return SendReceipt(existing.pk, conversation_id, replayed=True)
    now = timezone.now()
    decision = decide_send(
        SendRequest(
            channel=conversation.channel,
            purpose=conversation.purpose,
            conversation_state=conversation.state,
            now=now,
            last_inbound_at=conversation.last_inbound_at,
            template_category=template.category if template else None,
            template_status=template.approval_status if template else None,
            template_channel=template.channel if template else None,
            linked_to_patient=conversation.enrollment_id is not None,
        )
    )
    if not decision.allowed:
        raise InboxRefusedError(decision.reason)
    if conversation.purpose == "marketing" and not marketing_opted_in(
        enrollment_id=conversation.enrollment_id, channel=conversation.channel
    ):
        raise InboxRefusedError(Reason.MARKETING_OPT_IN_MISSING)
    with transaction.atomic():
        message = Message.objects.create(
            organization_id=conversation.organization_id,
            conversation=conversation,
            direction="out",
            body=rendered,
            template=template,
            template_parameters=json.dumps(list(parameters)) if template else None,
            sent_by_id=actor,
            command_id=command_id,
            created_at=now,
        )
        if decision.meter_category:
            MessageMeter.objects.create(
                organization_id=conversation.organization_id,
                message=message,
                source="expected",
                category=decision.meter_category,
            )
        enqueue_operation(
            OperationRequest(
                channel=conversation.channel,
                provider=PROVIDER_BY_CHANNEL[conversation.channel],
                clinic_id=clinic_id,
                subject_type=SUBJECT_TYPE,
                subject_id=message.pk,
                idempotency_key=command_id,
            )
        )
        conversation.last_activity_at = now
        conversation.revision += 1
        conversation.save(update_fields=["last_activity_at", "revision", "updated_at"])
        record_phase1_event(
            "comms.message.queued", clinic_id=clinic_id, affected_record_id=message.pk
        )
    publish_messages_hint(clinic_id)
    return SendReceipt(message.pk, conversation_id, replayed=False)


def retry_message(
    *, clinic_id: UUID, message_id: UUID, command_id: UUID
) -> SendReceipt:
    """Re-send a message whose delivery failed, under a fresh operation.

    The failed operation stays as evidence. The window and approval rules are
    checked again by the worker's recheck, so a retry never sends text after
    the window closed or from a template that was revoked.
    """
    message = Message.objects.filter(
        pk=message_id, conversation__clinic_id=clinic_id, direction="out"
    ).first()
    if message is None:
        raise InboxAccessDeniedError
    conversation, _ = _conversation(clinic_id, message.conversation_id, lock=True)
    state = message_states([message]).get(message.pk, "queued")
    if state != "failed":
        raise InboxRefusedError(Reason.NOT_FAILED)
    with transaction.atomic():
        enqueue_operation(
            OperationRequest(
                channel=conversation.channel,
                provider=PROVIDER_BY_CHANNEL[conversation.channel],
                clinic_id=clinic_id,
                subject_type=SUBJECT_TYPE,
                subject_id=message.pk,
                idempotency_key=command_id,
            )
        )
        if message.provider_state == "failed":
            Message.objects.filter(pk=message.pk).update(provider_state="")
        record_phase1_event(
            "comms.message.retried", clinic_id=clinic_id, affected_record_id=message.pk
        )
    publish_messages_hint(clinic_id)
    return SendReceipt(message.pk, conversation.pk, replayed=False)


def message_states(messages: Iterable[Message]) -> dict[UUID, str]:
    """Derive each outbound message's display state from outbox and provider facts."""
    ids = [m.pk for m in messages if m.direction == "out"]
    latest: dict[UUID, IntegrationOperation] = {}
    for operation in IntegrationOperation.objects.filter(
        subject_type=SUBJECT_TYPE, subject_id__in=ids
    ).order_by("created_at", "id"):
        latest[operation.subject_id] = operation
    return {
        m.pk: effective_delivery_state(
            operation_status=latest[m.pk].status if m.pk in latest else "pending",
            provider_state=m.provider_state,
        )
        for m in messages
        if m.direction == "out"
    }


def create_template_version(  # noqa: PLR0913 - one field per stored template term
    *,
    clinic_id: UUID,
    channel: str,
    name: str,
    language: str,
    category: str,
    purpose: str,
    body: str,
) -> UUID:
    """Draft the next immutable version of a template; managers only."""
    actor = _actor(clinic_id, MANAGER_ROLES)
    count = placeholder_count(body) if type(body) is str else None
    if (
        count is None
        or not 0 < len(body) <= MAX_TEMPLATE_BODY_CHARS
        or channel not in {"whatsapp", "email", "sms"}
    ):
        raise InboxRefusedError(Reason.INVALID_TEMPLATE)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"comms-template:{clinic_id}:{channel}:{name}:{language}"],
            )
        latest = TemplateVersion.objects.filter(
            clinic_id=clinic_id, channel=channel, name=name, language=language
        ).aggregate(top=Max("version"))["top"]
        clinic = _clinic_organization(clinic_id)
        template = TemplateVersion.objects.create(
            organization_id=clinic,
            clinic_id=clinic_id,
            channel=channel,
            name=name,
            language=language,
            version=(latest or 0) + 1,
            category=category,
            purpose=purpose,
            body=body,
            parameter_count=count,
            content_digest=hashlib.sha256(body.encode()).hexdigest(),
            created_by_id=actor,
        )
        record_phase1_event(
            "comms.template.created",
            clinic_id=clinic_id,
            affected_record_id=template.pk,
        )
    return template.pk


def _clinic_organization(clinic_id: UUID) -> UUID:
    from apps.identity.models import Clinic  # noqa: PLC0415 - avoid a wider import

    clinic = Clinic.objects.filter(pk=clinic_id).values_list(
        "organization_id", flat=True
    )
    organization = clinic.first()
    if organization is None:
        raise InboxAccessDeniedError
    return organization


def set_template_approval(
    *, clinic_id: UUID, template_version_id: UUID, status: str
) -> None:
    """Approve an email or SMS template as a manager; they have no provider review.

    WhatsApp templates are approved only by Meta; their status arrives through
    the authenticated template-status webhook, never from a staff click.
    """
    _actor(clinic_id, MANAGER_ROLES)
    template = (
        TemplateVersion.objects.select_for_update()
        .filter(pk=template_version_id, clinic_id=clinic_id)
        .first()
    )
    if template is None:
        raise InboxAccessDeniedError
    if template.channel == "whatsapp":
        raise InboxRefusedError(Reason.PROVIDER_APPROVAL_REQUIRED)
    apply_template_status(template, status)
    record_phase1_event(
        "comms.template.approval_changed",
        clinic_id=clinic_id,
        affected_record_id=template.pk,
    )


def apply_template_status(template: TemplateVersion, status: str) -> bool:
    """Move a template along the allowed approval path; False when not allowed."""
    from apps.comms.inbox_rules import template_transition_allowed  # noqa: PLC0415

    if not template_transition_allowed(current=template.approval_status, new=status):
        return False
    template.approval_status = status
    template.approval_changed_at = timezone.now()
    template.save(update_fields=["approval_status", "approval_changed_at"])
    return True


def create_quick_reply(*, clinic_id: UUID, title: str, body: str, purpose: str) -> UUID:
    """Save a reusable snippet that a human still reviews before sending."""
    actor = _actor(clinic_id, ROLES_BY_PURPOSE.get(purpose, ()))
    if (
        purpose not in {"transactional", "care"}
        or not title.strip()
        or not 0 < len(body.strip()) <= MAX_BODY_CHARS
    ):
        raise InboxRefusedError(Reason.INVALID_BODY)
    reply = QuickReply.objects.create(
        organization_id=_clinic_organization(clinic_id),
        clinic_id=clinic_id,
        title=title.strip()[:80],
        body=body.strip(),
        purpose=purpose,
        created_by_id=actor,
    )
    return reply.pk
