"""Read side of the staff inbox: what this actor may see, never more.

Visibility follows the same exact-role and patient-scoped rules as the commands.
Addresses are masked for display; the full destination exists only in the
encrypted column and in the worker's preparation step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.utils import timezone
from django.utils.translation import gettext, pgettext_lazy
from django.utils.translation import gettext_lazy as _

from apps.comms.inbox_rules import window_closes_at, window_open
from apps.comms.inbox_services import (
    CLINICIAN_ROLES,
    ROLES_BY_PURPOSE,
    InboxAccessDeniedError,
    _actor,
    _conversation,
    message_states,
)
from apps.comms.models import (
    Conversation,
    InternalNote,
    Message,
    QuickReply,
    SuggestedReply,
    TemplateVersion,
)
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.identity.models import CareTeamMembership, Clinic, UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_search import search_patients_exact

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

MAX_LIST: Final = 100
MAX_MESSAGES: Final = 200
MAX_QUICK_REPLIES: Final = 20
MAX_CHOICES: Final = 10
STATES: Final = ("open", "pending", "closed")
MASK_VISIBLE: Final = 4
CHANNEL_LABELS: Final = {
    "whatsapp": _("WhatsApp"),
    "email": _("Email"),
    "sms": _("SMS"),
    "portal": _("Portal"),
}
PURPOSE_LABELS: Final = {
    "transactional": pgettext_lazy("conversation purpose", "Transactional"),
    "care": pgettext_lazy("conversation purpose", "Care"),
    "marketing": _("Marketing"),
}
STATE_LABELS: Final = {
    "open": _("Open"),
    "pending": _("Pending"),
    "closed": pgettext_lazy("conversation state", "Closed"),
}


@dataclass(frozen=True, slots=True)
class ListRow:
    """One thread in the list, with enums already turned into labels."""

    conversation: Conversation
    label: str
    channel_label: str
    purpose_label: str
    state_label: str


@dataclass(frozen=True, slots=True)
class MessageRow:
    """One message as shown: body is decrypted only inside the actor's scope."""

    message: Message
    state: str
    template_name: str


@dataclass(frozen=True, slots=True)
class TemplateChoice:
    """An approved template with one input slot per placeholder in its body."""

    template: TemplateVersion
    slots: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PatientChoice:
    """One patient the actor may pick, named only after demographics authority."""

    enrollment_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class ConversationView:
    """Everything the detail page needs, assembled in one authorized read."""

    conversation: Conversation
    contact: str
    patient_label: str
    messages: tuple[MessageRow, ...]
    notes: tuple[InternalNote, ...]
    templates: tuple[TemplateChoice, ...]
    quick_replies: tuple[QuickReply, ...]
    free_text: bool
    channel_label: str
    purpose_label: str
    timezone: str
    suggestion: SuggestedReply | None
    window_open: bool
    window_closes_at: datetime | None


def mask_address(channel: str, address: str) -> str:
    """Show enough of a contact to recognise it, never the whole destination."""
    if channel == "email" and "@" in address:
        local, _, domain = address.partition("@")
        return f"{local[:1]}***@{domain}"
    if channel in {"whatsapp", "sms"}:
        return "•••• " + address[-MASK_VISIBLE:]
    return gettext("Patient portal")


def visible_purposes(clinic_id: UUID) -> tuple[str, ...]:
    """Purposes whose threads the current actor may open in this clinic."""
    try:
        actor = current_actor_id()
    except CurrentActorError as error:
        raise InboxAccessDeniedError from error
    held = set(
        UserClinicRole.objects.filter(user_id=actor, clinic_id=clinic_id).values_list(
            "role", flat=True
        )
    )
    return tuple(
        purpose for purpose, roles in ROLES_BY_PURPOSE.items() if held & set(roles)
    )


def is_clinician(clinic_id: UUID) -> bool:
    """Whether the actor holds a role that may take clinical questions."""
    try:
        actor = current_actor_id()
    except CurrentActorError:
        return False
    return UserClinicRole.objects.filter(
        user_id=actor, clinic_id=clinic_id, role__in=CLINICIAN_ROLES
    ).exists()


def list_conversations(
    *,
    clinic_id: UUID,
    state: str = "open",
    mine: bool = False,
    clinician_only: bool = False,
) -> list[ListRow]:
    """Return the actor's visible threads, newest activity first, bounded.

    A care thread also needs the actor's patient-scoped clinical authority, the
    same rule the thread page applies, so the list never names a patient the
    page would refuse.
    """
    purposes = visible_purposes(clinic_id)
    if not purposes:
        raise InboxAccessDeniedError
    queryset = Conversation.objects.filter(
        clinic_id=clinic_id,
        purpose__in=purposes,
        state=state if state in STATES else "open",
    ).select_related("enrollment__patient")
    if mine:
        queryset = queryset.filter(assignee_id=current_actor_id())
    if clinician_only:
        queryset = queryset.filter(needs_clinician=True)
    rows: list[ListRow] = []
    for conversation in queryset.order_by("-needs_clinician", "-last_activity_at")[
        :MAX_LIST
    ]:
        if conversation.purpose == "care":
            try:
                require_permission(
                    "clinical.read",
                    clinic_id=clinic_id,
                    patient_enrollment_id=conversation.enrollment_id,
                )
            except CurrentActorError:
                continue
        rows.append(
            ListRow(
                conversation=conversation,
                label=row_label(conversation),
                channel_label=str(CHANNEL_LABELS[conversation.channel]),
                purpose_label=str(PURPOSE_LABELS[conversation.purpose]),
                state_label=str(STATE_LABELS[conversation.state]),
            )
        )
    return rows


def _patient_name(conversation: Conversation) -> str:
    enrollment = conversation.enrollment
    return "" if enrollment is None else str(enrollment.patient.full_name)


def row_label(conversation: Conversation) -> str:
    """Patient name when verified, otherwise the masked contact."""
    return _patient_name(conversation) or mask_address(
        conversation.channel, conversation.contact_address
    )


def conversation_view(
    *, clinic_id: UUID, conversation_id: UUID, now: datetime
) -> ConversationView:
    """Load one thread the actor may handle, with its derived delivery states."""
    conversation, _ = _conversation(clinic_id, conversation_id)
    messages = list(
        Message.objects.filter(conversation=conversation)
        .select_related("template")
        .order_by("created_at", "id")[:MAX_MESSAGES]
    )
    states = message_states(messages)
    rows = tuple(
        MessageRow(
            message=m,
            state=states.get(m.pk, ""),
            template_name=m.template.name if m.template else "",
        )
        for m in messages
    )
    templates = (
        TemplateVersion.objects.filter(
            clinic_id=clinic_id,
            channel=conversation.channel,
            approval_status="approved",
        ).order_by("name", "language", "-version")
        if conversation.channel != "portal"
        else TemplateVersion.objects.none()
    )
    fitting = tuple(
        TemplateChoice(t, tuple(range(1, t.parameter_count + 1)))
        for t in templates
        if _fits(t, conversation.purpose)
    )
    suggestion = SuggestedReply.objects.filter(
        conversation=conversation, state="proposed"
    ).first()
    free_text = conversation.purpose != "marketing" and (
        conversation.channel != "whatsapp"
        or window_open(last_inbound_at=conversation.last_inbound_at, now=now)
    )
    return ConversationView(
        conversation=conversation,
        contact=mask_address(conversation.channel, conversation.contact_address),
        patient_label=_patient_name(conversation),
        messages=rows,
        notes=tuple(
            InternalNote.objects.filter(conversation=conversation).order_by(
                "created_at", "id"
            )[:MAX_MESSAGES]
        ),
        templates=fitting,
        quick_replies=(
            quick_replies(clinic_id, conversation.purpose) if free_text else ()
        ),
        free_text=free_text,
        channel_label=str(CHANNEL_LABELS[conversation.channel]),
        purpose_label=str(PURPOSE_LABELS[conversation.purpose]),
        timezone=_clinic_timezone(clinic_id),
        suggestion=suggestion,
        window_open=window_open(last_inbound_at=conversation.last_inbound_at, now=now),
        window_closes_at=window_closes_at(last_inbound_at=conversation.last_inbound_at),
    )


def quick_replies(clinic_id: UUID, purpose: str) -> tuple[QuickReply, ...]:
    """Active snippets for the thread's purpose; the thread read already authorized."""
    return tuple(
        QuickReply.objects.filter(
            clinic_id=clinic_id, purpose=purpose, active=True
        ).order_by("title", "id")[:MAX_QUICK_REPLIES]
    )


def care_team_patients(clinic_id: UUID, query: str = "") -> tuple[PatientChoice, ...]:
    """Patients on the signed-in clinician's active care team, optionally by name.

    Clinicians have no registry search: their own care-team scope is the only
    list of patients they may start a care thread with. The thread commands
    still recheck patient-scoped clinical authority on every call.
    """
    actor = _actor(clinic_id, CLINICIAN_ROLES)
    try:
        require_permission("demographics.read", clinic_id=clinic_id)
    except CurrentActorError as error:
        raise InboxAccessDeniedError from error
    now = timezone.now()
    memberships = CareTeamMembership.objects.filter(
        clinic_id=clinic_id,
        user_id=actor,
        revoked_at__isnull=True,
        valid_from__lte=now,
    ).exclude(valid_to__lte=now)
    wanted = " ".join(query.lower().split())
    choices: dict[UUID, PatientChoice] = {}
    for enrollment in (
        PatientClinicEnrollment.objects.filter(
            pk__in=memberships.values("patient_enrollment_id"), clinic_id=clinic_id
        )
        .select_related("patient")
        .order_by("id")[:100]
    ):
        name = str(enrollment.patient.full_name)
        if wanted in " ".join(name.lower().split()):
            choices[enrollment.pk] = PatientChoice(enrollment.pk, name)
    return tuple(choices.values())[:MAX_CHOICES]


def search_patient_choices(clinic_id: UUID, query: str) -> tuple[PatientChoice, ...]:
    """Exact-name registry search; the intake service decides who may run it."""
    page = search_patients_exact(clinic_id=clinic_id, query=query, limit=MAX_CHOICES)
    return tuple(
        PatientChoice(item.enrollment_id, item.display_name or item.full_name)
        for item in page.items
    )


def _clinic_timezone(clinic_id: UUID) -> str:
    zone = (
        Clinic.objects.filter(pk=clinic_id).values_list("timezone", flat=True).first()
    )
    return str(zone or "UTC")


def _fits(template: TemplateVersion, purpose: str) -> bool:
    from apps.comms.inbox_rules import template_fits_purpose  # noqa: PLC0415

    return template_fits_purpose(category=template.category, purpose=purpose)


def parse_uuid(raw: object) -> UUID | None:
    """Return a UUID or None; request values never raise past this boundary."""
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def parameters_from(post: Mapping[str, object], count: int) -> tuple[str, ...]:
    """Collect ``p1..pN`` template parameters in positional order."""
    return tuple(str(post.get(f"p{index}", "")) for index in range(1, count + 1))
