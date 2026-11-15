"""Render a masked support snapshot through closed, translated label maps.

Every value shown is a label from these maps, a count or a whole-minute age;
nothing is a stored name, identifier or free text.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from ops.release import profiles

from apps.identity import enterprise_authority as clock
from apps.identity.models import UserClinicRole

if TYPE_CHECKING:
    from datetime import datetime

    from django.utils.functional import Promise

    from apps.identity.enterprise_support_access import SupportSnapshot

SECONDS_PER_MINUTE: Final = 60
PROVIDER_STATES: Final[dict[str, Promise]] = {
    "researched": _("Researched"),
    "selected_in_plan": _("Selected in the plan"),
    "approved_to_test": _("Approved for testing"),
    "sandbox": _("Sandbox"),
    "production_authorized": _("Authorized for production"),
    "activated": _("Activated"),
    "degraded": _("Degraded"),
    "revoked": _("Revoked"),
    "unavailable": _("Unavailable"),
}
CHANNELS: Final[dict[str, Promise]] = {
    "": _("Action (no message channel)"),
    "sms": _("SMS"),
    "email": _("Email"),
    "whatsapp": _("WhatsApp"),
    "video": _("Video"),
    "portal": _("Portal"),
}
OUTBOX_STATUSES: Final[dict[str, Promise]] = {
    "pending": _("Waiting to send"),
    "in_progress": _("Sending"),
    "succeeded": _("Sent, awaiting confirmation"),
    "delivered": _("Delivered"),
    "failed": _("Failed"),
    "cancelled": _("Cancelled"),
}
TASK_STATES: Final[dict[str, Promise]] = {
    "open": _("Open"),
    "assigned": _("Assigned"),
    "in_progress": _("In progress"),
    "done": _("Done"),
    "cancelled": _("Cancelled"),
}
OPEN_TASK_STATES: Final = frozenset({"open", "assigned", "in_progress"})
WAITING_STATUSES: Final = frozenset({"pending", "in_progress", "succeeded"})


def _minutes(later: datetime, earlier: datetime) -> int:
    return max(0, int((later - earlier).total_seconds() // SECONDS_PER_MINUTE))


def support_context(snapshot: SupportSnapshot) -> dict[str, object]:
    """Map the snapshot to template rows; an unknown stored value is an error."""
    now = clock.utc_now()
    profile = profiles.PROFILES.get(
        os.environ.get(profiles.PROFILE_ENV, profiles.DEFAULT_LIVE_PROFILE)
    )
    enabled = (
        profile.providers
        if profile is not None and settings.CLINIC_DATA_MODE == "live"
        else ()
    )
    return {
        "providers": [
            {
                "capability": row["capability"],
                "state": PROVIDER_STATES[row["state"]],
                "available": (
                    row["state"] == "activated" and row["capability"] in enabled
                ),
            }
            for row in snapshot.providers
        ],
        "queues": [
            {
                "channel": CHANNELS[row.channel],
                "status": OUTBOX_STATUSES[row.status],
                "count": row.count,
                "waiting": row.status in WAITING_STATUSES,
                "oldest_minutes": _minutes(now, row.oldest_at),
            }
            for row in snapshot.queues
        ],
        "tasks": [
            {
                "state": TASK_STATES[row.state],
                "count": row.count,
                "overdue": row.state in OPEN_TASK_STATES and row.earliest_due_at < now,
            }
            for row in snapshot.tasks
        ],
        "staff": [
            {"role": UserClinicRole.Role(row.role).label, "count": row.count}
            for row in snapshot.staff
        ],
        "units": snapshot.units,
        "minutes_left": (
            _minutes(snapshot.expires_at, now) if snapshot.expires_at else 0
        ),
    }
