"""Front-desk agent records (todo 54, AI-04): reviewed answers and thread state.

A knowledge article is clinic administrative content (hours, prices, services,
preparation, policies) that a clinic administrator approves version by version;
it never holds patient data. A front-desk thread is the agent's state for one
comms conversation: verification, the one slot offer it may book after the
patient's explicit acceptance, and the human handoff. A turn is the provenance of
one agent reply: intent, tool, outcome, cited article version and policy.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.tenancy.models import TenantScopedModel

TOPICS: Final = ("hours", "prices", "services", "preparation", "policies")
ARTICLE_STATES: Final = ("draft", "approved", "retired")
THREAD_STATES: Final = ("active", "handoff_pending", "resolved")
HANDOFF_REASONS: Final = (
    "",
    "urgent",
    "clinical",
    "human_request",
    "kb_missing",
    "verification_failed",
    "policy_limit",
)
TURN_STATES: Final = ("pending", "finishing", "done")
INTENTS: Final = (
    "",
    "greeting",
    "information",
    "appointments",
    "reschedule",
    "birth_date",
    "accept",
    "decline",
    "human",
    "urgent",
    "clinical",
    "unknown",
)
TOOLS: Final = (
    "",
    "scheduling.hold",
    "scheduling.book_approved",
    "comms.send_approved_link",
)
OUTCOMES: Final = (
    "",
    "answered",
    "challenge",
    "verified",
    "not_verified",
    "offered",
    "booked",
    "no_availability",
    "waitlist",
    "refused",
    "handoff",
)
MAX_BODY_CHARS: Final = 600
MAX_KEYWORDS: Final = 12


class KnowledgeArticle(TenantScopedModel):
    """One version of a clinic-approved administrative answer (D-15)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    key = models.CharField(max_length=40)
    version = models.PositiveIntegerField()
    topic = models.CharField(max_length=16)
    title = models.CharField(max_length=120)
    body = models.CharField(max_length=MAX_BODY_CHARS)
    keywords = ArrayField(models.CharField(max_length=40), size=MAX_KEYWORDS)
    # An approved https link the agent may send with this answer
    # (comms.send_approved_link); approved together with the text.
    link = models.URLField(max_length=200, blank=True, default="")
    state = models.CharField(max_length=10, default="draft")
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True)
    retired_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Versions are per clinic and key; approval is recorded exactly once."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "key", "version"), name="ai_article_version"
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_article_scope"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1)
                & models.Q(topic__in=TOPICS)
                & (
                    models.Q(state="draft", approved_by__isnull=True)
                    | models.Q(
                        state__in=("approved", "retired"),
                        approved_by__isnull=False,
                        approved_at__isnull=False,
                    )
                ),
                name="ai_article_terms",
            ),
        ]


class FrontDeskThread(TenantScopedModel):
    """The agent's state for one conversation; never a copy of message text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    conversation = models.OneToOneField(
        "comms.Conversation", on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=16, default="active")
    handoff_reason = models.CharField(max_length=24, blank=True, default="")
    verified_enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    verified_until = models.DateTimeField(null=True)
    # The channel session minted by a successful challenge; appointment reads
    # and changes run under it, through the patient booking services.
    session = models.ForeignKey(
        "intake.PatientSession",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    challenge_attempts = models.PositiveSmallIntegerField(default=0)
    challenge_until = models.DateTimeField(null=True)
    # The one held offer: an existing appointment to move (or none for a new
    # booking) and the exact real slot the patient must accept.
    offered_appointment = models.ForeignKey(
        "scheduling.Appointment",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    offered_practitioner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    offered_start = models.DateTimeField(null=True)
    offered_until = models.DateTimeField(null=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """States carry exactly the attribution they need."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_frontdesk_scope"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="active", handoff_reason="")
                    | models.Q(state="handoff_pending") & ~models.Q(handoff_reason="")
                    | models.Q(state="resolved", resolved_by__isnull=False)
                )
                & models.Q(challenge_attempts__lte=3),
                name="ai_frontdesk_state",
            ),
        ]


class FrontDeskTurn(TenantScopedModel):
    """Provenance of one agent reply: no text, only closed values and pointers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    thread = models.ForeignKey(
        FrontDeskThread, on_delete=models.PROTECT, related_name="turns"
    )
    inbound = models.OneToOneField(
        "comms.Message", on_delete=models.PROTECT, related_name="+"
    )
    reply = models.OneToOneField(
        "comms.Message", null=True, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=10, default="pending")
    intent = models.CharField(max_length=16, blank=True, default="")
    tool = models.CharField(max_length=32, blank=True, default="")
    outcome = models.CharField(max_length=16, blank=True, default="")
    article = models.ForeignKey(
        KnowledgeArticle, null=True, on_delete=models.PROTECT, related_name="+"
    )
    policy = models.ForeignKey(
        "ai.AgentPolicyVersion", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Closed vocabularies only."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_turn_scope"
            ),
            models.CheckConstraint(
                condition=models.Q(intent__in=INTENTS)
                & models.Q(tool__in=TOOLS)
                & models.Q(outcome__in=OUTCOMES)
                & (
                    models.Q(state="pending", intent="", outcome="", reply__isnull=True)
                    | models.Q(state__in=("finishing", "done"), reply__isnull=False)
                    & ~models.Q(intent="")
                    & ~models.Q(outcome="")
                ),
                name="ai_turn_terms",
            ),
        ]
