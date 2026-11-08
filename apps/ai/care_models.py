"""Care-coordination proposals (AI-05): validated model items awaiting a human.

A row holds only a closed kind, a window and references. Model text never lands
here: the encrypted gateway output keeps it until retention purges it.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.db import models

from apps.tenancy.models import TenantScopedModel
from apps.workflows.care.vocab import ACTION_KINDS

KINDS: Final = (*ACTION_KINDS, "unsupported")
AUDIENCES: Final = ("administrative", "clinical")
STATES: Final = ("pending", "refused", "approved", "accepted", "rejected", "stale")
REASON_CODES: Final = (
    "",
    "kind_forbidden",
    "window_invalid",
    "malformed",
    "duplicate",
    "source_superseded",
)


def _in(field: str, values: tuple[str, ...]) -> models.Q:
    return models.Q(**{f"{field}__in": values})


class CareProposal(TenantScopedModel):
    """One proposed action from one model run over one approved plan version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    invocation = models.ForeignKey(
        "ai.AIInvocation", on_delete=models.PROTECT, related_name="+"
    )
    plan = models.ForeignKey(
        "workflows.CarePlan", on_delete=models.PROTECT, related_name="+"
    )
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT, related_name="+"
    )
    source_version = models.ForeignKey(
        "ehr.ClinicalDocumentVersion", on_delete=models.PROTECT, related_name="+"
    )
    plan_revision = models.PositiveIntegerField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    position = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=24)
    audience = models.CharField(max_length=16)
    window_start = models.DateTimeField(null=True)
    window_end = models.DateTimeField(null=True)
    state = models.CharField(max_length=12)
    reason_code = models.CharField(max_length=24, blank=True, default="")
    action_proposal = models.OneToOneField(
        "ai.ActionProposal", null=True, on_delete=models.PROTECT, related_name="+"
    )
    care_action = models.OneToOneField(
        "workflows.CareAction", null=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        """Closed vocabularies; one row per model item; decisions keep their links."""

        db_table = "ai_careproposal"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("invocation", "position"), name="ai_care_position"
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_care_scope"
            ),
            models.CheckConstraint(
                condition=_in("kind", KINDS)
                & _in("audience", AUDIENCES)
                & _in("state", STATES)
                & _in("reason_code", REASON_CODES),
                name="ai_care_vocabulary",
            ),
            # A refusal always names its closed reason and may have no window;
            # every other row is a whole window, and only staleness has a reason.
            models.CheckConstraint(
                condition=(models.Q(state="refused") & ~models.Q(reason_code=""))
                | models.Q(
                    state="stale",
                    reason_code="source_superseded",
                    window_start__isnull=False,
                    window_end__isnull=False,
                    window_end__gt=models.F("window_start"),
                )
                | models.Q(
                    state__in=("pending", "approved", "accepted", "rejected"),
                    reason_code="",
                    window_start__isnull=False,
                    window_end__isnull=False,
                    window_end__gt=models.F("window_start"),
                ),
                name="ai_care_window",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("pending", "refused"),
                    decided_by__isnull=True,
                    decided_at__isnull=True,
                    action_proposal__isnull=True,
                    care_action__isnull=True,
                )
                | models.Q(
                    state="approved",
                    audience="administrative",
                    decided_by__isnull=False,
                    decided_at__isnull=False,
                    action_proposal__isnull=False,
                    care_action__isnull=False,
                )
                | models.Q(
                    state="accepted",
                    audience="clinical",
                    decided_by__isnull=False,
                    decided_at__isnull=False,
                    action_proposal__isnull=True,
                    care_action__isnull=False,
                )
                | models.Q(
                    state__in=("rejected", "stale"),
                    decided_by__isnull=False,
                    decided_at__isnull=False,
                    action_proposal__isnull=True,
                    care_action__isnull=True,
                ),
                name="ai_care_decision",
            ),
        ]
        indexes: ClassVar = [
            models.Index(fields=("clinic", "plan", "state"), name="ai_care_plan_state")
        ]
