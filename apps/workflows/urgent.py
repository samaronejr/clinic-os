"""Urgent patient concerns in an accountable human queue (todo 55, scenario 20).

An urgent portal message (``classify_inbound``) becomes one concern through the
patient bridge in the reply's own transaction. The clinic's coverage policy
names the responsible roles, the hours and the response window in business
hours; the deadline is derived from the report time and that policy. An open
concern past its deadline is overdue: it escalates to the clinic's managers and
rises above every other item. Nothing replies to the patient automatically: the
portal only states the configured window honestly, and without a policy it
claims no coverage at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from heapq import nsmallest
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop, ngettext

from apps.audit.events import build_phase1_audit_event
from apps.audit.services import AuditTrustedContext, _content_hash, record_phase1_event
from apps.comms.portal_messages import portal_message_scope
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
    require_permission,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.inbox import InboxItem, register_inbox_feed
from apps.workflows.urgent_models import (
    MAX_RESPONSE_HOURS,
    RESPONSIBLE_ROLES,
    UrgentConcern,
    UrgentCoveragePolicy,
)
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError

if TYPE_CHECKING:
    from apps.identity.current_context import ClinicRoles
    from apps.workflows.inbox import InboxScope

__all__ = (
    "ConcernRow",
    "Coverage",
    "acknowledge_urgent_concern",
    "coverage_settings",
    "coverage_text",
    "current_coverage",
    "patient_coverage_text",
    "report_urgent_message",
    "resolve_urgent_concern",
    "set_urgent_coverage",
    "urgent_concerns",
    "utc_now",
)

FEED: Final = "urgent"
REPORTED_EVENT: Final = "workflows.urgent_concern.reported"
_SUNDAY: Final = 6
NO_COVERAGE: Final = gettext_noop(
    "This clinic does not watch messages continuously. In an emergency, call 192."
)
WEEKDAY_LABELS: Final = (
    gettext_noop("Mon"),
    gettext_noop("Tue"),
    gettext_noop("Wed"),
    gettext_noop("Thu"),
    gettext_noop("Fri"),
    gettext_noop("Sat"),
    gettext_noop("Sun"),
)


def utc_now() -> datetime:
    """Return the queue's clock; tests replace this one function."""
    return timezone.now()


@dataclass(frozen=True, slots=True)
class Coverage:
    """The terms of one coverage policy version, all clinic-local."""

    version: int
    weekdays: tuple[int, ...]
    opens_at: time
    closes_at: time
    response_hours: int
    responsible_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConcernRow:
    """One queue row: state and timing only; the text stays in the thread."""

    concern_id: UUID
    conversation_id: UUID
    revision: int
    state: Literal["open", "acknowledged", "resolved"]
    reported_at: datetime
    due_at: datetime | None
    overdue: bool
    responsible: bool


def _coverage(row: UrgentCoveragePolicy) -> Coverage:
    return Coverage(
        row.version,
        tuple(row.weekdays),
        row.opens_at,
        row.closes_at,
        row.response_hours,
        tuple(row.responsible_roles),
    )


def due_at(
    reported_at: datetime, coverage: Coverage | None, zone: ZoneInfo
) -> datetime | None:
    """Add the response window in covered business minutes; None without policy."""
    if coverage is None or not coverage.weekdays:
        return None
    remaining = timedelta(hours=coverage.response_hours)
    cursor = reported_at.astimezone(zone)
    weekly = (
        datetime.combine(cursor.date(), coverage.closes_at)
        - datetime.combine(cursor.date(), coverage.opens_at)
    ) * len(coverage.weekdays)
    while True:
        for _day in range(7):
            day = cursor.date()
            if day.weekday() in coverage.weekdays:
                opens = datetime.combine(day, coverage.opens_at, tzinfo=zone)
                closes = datetime.combine(day, coverage.closes_at, tzinfo=zone)
                start = max(opens, cursor)
                if start < closes:
                    if remaining <= closes - start:
                        return (start + remaining).astimezone(UTC)
                    remaining -= closes - start
            cursor = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
        # The partial first week is consumed. Skip complete weekly windows,
        # leaving a positive remainder that fits in the next covered week.
        weeks = (remaining - timedelta.resolution) // weekly
        cursor += timedelta(weeks=weeks)
        remaining -= weekly * weeks


def _zone(clinic_id: UUID) -> ZoneInfo:
    return ZoneInfo(str(Clinic.objects.get(pk=clinic_id).timezone))


def _latest(clinic_id: UUID) -> Coverage | None:
    row = (
        UrgentCoveragePolicy.objects.filter(clinic_id=clinic_id)
        .order_by("-version")
        .first()
    )
    return None if row is None else _coverage(row)


def current_coverage(*, clinic_id: UUID) -> Coverage | None:
    """Return the clinic's latest coverage policy as the clinic's staff read it."""
    require_permission("demographics.read", clinic_id=clinic_id)
    return _latest(clinic_id)


def coverage_settings(*, clinic_id: UUID) -> Coverage | None:
    """Return the latest coverage policy to whoever configures the clinic."""
    require_permission("configuration.clinic", clinic_id=clinic_id)
    return _latest(clinic_id)


@transaction.atomic
def set_urgent_coverage(  # noqa: PLR0913 - the closed policy terms
    *,
    clinic_id: UUID,
    expected_version: int,
    weekdays: tuple[int, ...],
    opens_at: time,
    closes_at: time,
    response_hours: int,
    responsible_roles: tuple[str, ...],
) -> Coverage:
    """Publish the next coverage version; only clinic configuration may."""
    actor = require_permission("configuration.clinic", clinic_id=clinic_id)
    days = tuple(sorted(set(weekdays)))
    roles = tuple(sorted(set(responsible_roles)))
    if (
        not days
        or any(type(day) is not int or not 0 <= day <= _SUNDAY for day in days)
        or type(opens_at) is not time
        or type(closes_at) is not time
        or opens_at.tzinfo is not None
        or closes_at.tzinfo is not None
        or opens_at.second != 0
        or opens_at.microsecond != 0
        or closes_at.second != 0
        or closes_at.microsecond != 0
        or closes_at <= opens_at
        or type(response_hours) is not int
        or not 1 <= response_hours <= MAX_RESPONSE_HOURS
        or not roles
        or not set(roles) <= set(RESPONSIBLE_ROLES)
        or type(expected_version) is not int
    ):
        raise WorkflowInputError
    # Versions are append-only rows (no UPDATE grant, so no row lock): one
    # publication per clinic at a time decides the next version.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"workflows-urgent-coverage:{clinic_id}"],
        )
    latest = (
        UrgentCoveragePolicy.objects.filter(clinic_id=clinic_id)
        .order_by("-version")
        .first()
    )
    if (latest.version if latest else 0) != expected_version:
        raise WorkflowConflictError
    clinic = Clinic.objects.get(pk=clinic_id)
    row = UrgentCoveragePolicy.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        version=expected_version + 1,
        weekdays=list(days),
        opens_at=opens_at,
        closes_at=closes_at,
        response_hours=response_hours,
        responsible_roles=list(roles),
        created_by_id=actor,
    )
    record_phase1_event(
        "workflows.urgent_coverage.published",
        clinic_id=clinic_id,
        affected_record_id=row.pk,
    )
    return _coverage(row)


def coverage_text(coverage: Coverage | None) -> tuple[str, ...]:
    """Return the honest patient sentences for a policy; none claims nothing."""
    if coverage is None:
        return (_(NO_COVERAGE),)
    days = ", ".join(_(WEEKDAY_LABELS[day]) for day in coverage.weekdays)
    return (
        _(
            "Urgent messages are read by the clinic team on %(days)s, "
            "from %(opens)s to %(closes)s."
        )
        % {
            "days": days,
            "opens": coverage.opens_at.strftime("%H:%M"),
            "closes": coverage.closes_at.strftime("%H:%M"),
        },
        ngettext(
            "Reply within %(hours)s business hour; in an emergency, call 192.",
            "Reply within %(hours)s business hours; in an emergency, call 192.",
            coverage.response_hours,
        )
        % {"hours": coverage.response_hours},
    )


def patient_coverage_text() -> tuple[str, ...]:
    """Read the session clinic's policy for its patients; never a staff read."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.workflows_patient_coverage()")
        row = cursor.fetchone()
    if row is None:
        return coverage_text(None)
    weekdays, opens_at, closes_at, hours = row
    return coverage_text(
        Coverage(0, tuple(weekdays), opens_at, closes_at, int(hours), ())
    )


def report_urgent_message(message_id: UUID) -> None:
    """Patient bridge: one urgent reply opens one concern, audited once.

    The SQL bridge derives organization, clinic and enrollment from the live
    patient session and admits only a message that session wrote in this
    transaction, so no caller-supplied scope is trusted here.
    """
    scope = portal_message_scope()
    if scope is None:
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.workflows_report_urgent(%s)", [message_id])
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], UUID):
        return
    event = build_phase1_audit_event(
        REPORTED_EVENT, clinic_id=scope.clinic_id, affected_record_id=row[0]
    )
    content_hash = _content_hash(
        event.event,
        AuditTrustedContext(
            organization_id=scope.organization_id, actor_user_id=scope.session_id
        ),
        dict(event.payload),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_urgent_audit(%s,%s,%s)",
            [row[0], event.event.occurred_at_utc, content_hash],
        )


def _responsible(concern: UrgentConcern) -> bool:
    roles = list(concern.policy.responsible_roles) if concern.policy else None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_urgent_responsible(%s,%s::varchar[])",
            [concern.clinic_id, roles],
        )
        row = cursor.fetchone()
    return row is not None and row[0] is True


def _row(concern: UrgentConcern, zone: ZoneInfo, now: datetime) -> ConcernRow:
    deadline = due_at(
        concern.reported_at,
        _coverage(concern.policy) if concern.policy else None,
        zone,
    )
    return ConcernRow(
        concern.pk,
        concern.conversation_id,
        concern.revision,
        cast("Literal['open', 'acknowledged', 'resolved']", concern.state),
        concern.reported_at,
        deadline,
        concern.state == "open" and deadline is not None and now >= deadline,
        _responsible(concern),
    )


def urgent_concerns(*, clinic_id: UUID) -> tuple[ConcernRow, ...]:
    """List open and acknowledged concerns the actor may handle or must escalate.

    Responsible roles see every unresolved concern; managers outside those roles
    see only the overdue ones. Overdue concerns come first, then the oldest.
    """
    current_actor_id()
    zone = _zone(clinic_id)
    now = utc_now()
    rows = (
        _row(concern, zone, now)
        for concern in UrgentConcern.objects.select_related("policy")
        .filter(clinic_id=clinic_id, state__in=("open", "acknowledged"))
        .order_by("reported_at", "pk")
        .iterator(chunk_size=200)
    )
    visible = (row for row in rows if row.responsible or row.overdue)
    return tuple(
        nsmallest(200, visible, key=lambda row: (not row.overdue, row.reported_at))
    )


def _decide(
    *, clinic_id: UUID, concern_id: UUID, expected_revision: int, state: str
) -> ConcernRow:
    if type(concern_id) is not UUID or type(expected_revision) is not int:
        raise WorkflowAccessDeniedError
    concern = (
        # Lock the concern row only: its policy is a nullable outer join.
        UrgentConcern.objects.select_for_update(of=("self",))
        .select_related("policy")
        .filter(pk=concern_id, clinic_id=clinic_id)
        .first()
    )
    if concern is None:
        raise WorkflowAccessDeniedError
    roles = tuple(concern.policy.responsible_roles if concern.policy else ())
    actor = _responsible_actor(
        clinic_id,
        tuple(UserClinicRole.Role(role) for role in roles)
        or (UserClinicRole.Role.CLINIC_MANAGER,),
    )
    expected = {"acknowledged": "open", "resolved": "acknowledged"}[state]
    if concern.revision != expected_revision or concern.state != expected:
        raise WorkflowConflictError
    concern.state = state
    if state == "acknowledged":
        concern.acknowledged_by_id = actor
    else:
        concern.resolved_by_id = actor
    concern.revision += 1
    concern.save(
        update_fields=(
            "state",
            "acknowledged_by",
            "acknowledged_at",
            "resolved_by",
            "resolved_at",
            "revision",
        )
    )
    record_phase1_event(
        f"workflows.urgent_concern.{state}",
        clinic_id=clinic_id,
        affected_record_id=concern.pk,
    )
    concern.refresh_from_db()
    return _row(concern, _zone(clinic_id), utc_now())


def _responsible_actor(clinic_id: UUID, roles: ClinicRoles) -> UUID:
    """Admit only the policy's responsible roles (managers without a policy)."""
    return require_current_actor_clinic_roles(clinic_id, roles)


@transaction.atomic
def acknowledge_urgent_concern(
    *, clinic_id: UUID, concern_id: UUID, expected_revision: int
) -> ConcernRow:
    """Take responsibility for an open concern as a responsible role."""
    return _decide(
        clinic_id=clinic_id,
        concern_id=concern_id,
        expected_revision=expected_revision,
        state="acknowledged",
    )


@transaction.atomic
def resolve_urgent_concern(
    *, clinic_id: UUID, concern_id: UUID, expected_revision: int
) -> ConcernRow:
    """Close an acknowledged concern after a human handled it in the thread."""
    return _decide(
        clinic_id=clinic_id,
        concern_id=concern_id,
        expected_revision=expected_revision,
        state="resolved",
    )


def urgent_items(scope: InboxScope) -> tuple[InboxItem, ...]:
    """Clinical Inbox feed: the actor's concerns, overdue and critical first."""
    try:
        rows = urgent_concerns(clinic_id=scope.clinic_id)
    except CurrentActorError:
        return ()
    return tuple(
        InboxItem(
            kind=FEED,
            record_id=row.concern_id,
            revision=row.revision,
            title=_("Urgent patient message"),
            target_id=row.conversation_id,
            due_at=row.due_at,
            priority="urgent",
            critical=row.overdue,
            action="open_urgent",
            overdue=row.overdue,
        )
        for row in rows[: scope.limit]
    )


def register_urgent_feed() -> None:
    """Register the urgent-concern feed in the Clinical Inbox."""
    register_inbox_feed(
        FEED, urgent_items, label=gettext_noop("Urgent concerns"), position=5
    )
