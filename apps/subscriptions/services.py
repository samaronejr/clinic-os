"""Server-owned commercial decisions; no clinical operation calls this gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import DatabaseError, connection, transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.identity.management.base import assert_owner_database_role
from apps.subscriptions.collection import simulate_event
from apps.subscriptions.models import CORE_FEATURES, METERS, Subscription, UsageRollup

if TYPE_CHECKING:
    from datetime import datetime

__all__ = (
    "cancel_subscription",
    "change_plan",
    "entitlement_status",
    "receive_synthetic_event",
    "subscription_snapshot",
    "usage_report",
)


class SubscriptionDeniedError(Exception):
    """Return one non-identifying denial for every unavailable selector."""


class SubscriptionConflictError(Exception):
    """Reject a stale revision or a reused key with different command terms."""


class SubscriptionInputError(ValueError):
    """Reject input outside the bounded commercial contract."""


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    """Expose commercial state without identity or clinical records."""

    subscription_id: UUID
    state: str
    revision: int
    plan_code: str
    provisional: bool
    trial_ends_at: datetime
    grace_ends_at: datetime | None
    cancellation_at: datetime | None
    seats: int
    notice_code: str


@dataclass(frozen=True, slots=True)
class EntitlementStatus:
    """A closed availability decision with transparent quota and alerts."""

    available: bool
    used: int
    quota: int
    alert: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The immutable result revision survives later commands and replays."""

    subscription_id: UUID
    revision: int
    state: str


def _scope(clinic_id: UUID, *, manage: bool = False) -> UUID:
    try:
        if manage:
            require_permission("configuration.organization", clinic_id=clinic_id)
        else:
            current_actor_id()
    except CurrentActorError:
        raise SubscriptionDeniedError from None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.subscription_scope(%s,%s)", [clinic_id, manage]
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], UUID):
        raise SubscriptionDeniedError
    return row[0]


def subscription_snapshot(*, clinic_id: UUID) -> SubscriptionSnapshot:
    """Read current commercial state; a restricted subscription still reads."""
    organization = _scope(clinic_id)
    subscription = (
        Subscription.objects.select_related("plan")
        .filter(organization_id=organization)
        .first()
    )
    if subscription is None:
        raise SubscriptionDeniedError
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.subscription_seats(%s)", [organization])
        (row,) = cursor.fetchall()
    return SubscriptionSnapshot(
        subscription.pk,
        subscription.state,
        subscription.revision,
        subscription.plan.code,
        subscription.plan.provisional,
        subscription.trial_ends_at,
        subscription.grace_ends_at,
        subscription.cancellation_at,
        int(row[0]),
        {
            "past_due": "payment_past_due",
            "grace": "payment_grace_deadline",
            "restricted": "manual_workflow_available",
            "cancelled": "offboarding_scheduled",
            "offboarded": "export_and_records_available",
        }.get(subscription.state, ""),
    )


def entitlement_status(
    *, clinic_id: UUID, meter: str, quantity: int = 1
) -> EntitlementStatus:
    """Resolve a server allowance; quota exhaustion offers the manual workflow."""
    _scope(clinic_id)
    if (
        meter not in (*METERS, *CORE_FEATURES)
        or type(quantity) is not int
        or not 0 <= quantity <= 2**63 - 1
    ):
        raise SubscriptionInputError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.subscription_allowance(%s,%s,%s)",
            [clinic_id, meter, quantity],
        )
        row = cursor.fetchone()
    if row is None:
        raise SubscriptionDeniedError
    available, used, quota, reason = row
    return EntitlementStatus(
        bool(available),
        int(used),
        int(quota),
        int(quota) > 0 and int(used) * 5 >= int(quota) * 4,
        str(reason),
    )


def usage_report(*, clinic_id: UUID) -> tuple[tuple[str, str, int], ...]:
    """Return UTC monthly quantities without resolving source or patient content."""
    organization = _scope(clinic_id, manage=True)
    return tuple(
        (str(period), meter, quantity)
        for period, meter, quantity in UsageRollup.objects.filter(
            organization_id=organization
        )
        .order_by("period", "meter")
        .values_list("period", "meter", "quantity")
    )


def _command(
    *,
    clinic_id: UUID,
    action: str,
    plan_code: str,
    expected_revision: int,
    command_id: UUID,
) -> CommandResult:
    _scope(clinic_id, manage=True)
    if type(expected_revision) is not int or not 1 <= expected_revision <= 2**31 - 1:
        raise SubscriptionInputError
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.subscription_command(%s,%s,%s,%s,%s)",
                [clinic_id, action, plan_code, expected_revision, command_id],
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        if getattr(error.__cause__, "sqlstate", None) == "42501":
            raise SubscriptionDeniedError from None
        raise
    if row is None or row[0] is None:
        raise SubscriptionConflictError
    subscription_id, revision, state, changed = row
    if changed:
        record_phase1_event(
            "subscriptions.subscription.changed",
            clinic_id=clinic_id,
            affected_record_id=subscription_id,
        )
    return CommandResult(subscription_id, int(revision), str(state))


@transaction.atomic
def change_plan(
    *, clinic_id: UUID, plan_code: str, expected_revision: int, command_id: UUID
) -> CommandResult:
    """Select an owner-published package; callers cannot supply entitlements."""
    if plan_code not in {"core_operations", "intelligence_usage", "network_governance"}:
        raise SubscriptionInputError
    return _command(
        clinic_id=clinic_id,
        action="plan_changed",
        plan_code=plan_code,
        expected_revision=expected_revision,
        command_id=command_id,
    )


@transaction.atomic
def cancel_subscription(
    *, clinic_id: UUID, expected_revision: int, command_id: UUID
) -> CommandResult:
    """Schedule retained offboarding without changing clinical or export authority."""
    return _command(
        clinic_id=clinic_id,
        action="cancel",
        plan_code="",
        expected_revision=expected_revision,
        command_id=command_id,
    )


@transaction.atomic
def receive_synthetic_event(
    *, clinic_id: UUID, subscription_id: UUID, event_id: UUID, action: str
) -> CommandResult:
    """Owner-only rehearsal receipt; no web callback or payload tenant is trusted."""
    assert_owner_database_role()
    receipt = simulate_event(
        clinic_id=clinic_id,
        subscription_id=subscription_id,
        event_id=event_id,
        action=action,
    )
    # The fixed resolver rechecks the owner's current organization-wide operator.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.subscription_platform_event(%s,%s,%s,%s)",
            [clinic_id, receipt.subscription_id, receipt.event_id, receipt.action],
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise SubscriptionConflictError
    selected, revision, state, changed = row
    if changed:
        record_phase1_event(
            "subscriptions.subscription.changed",
            clinic_id=clinic_id,
            affected_record_id=selected,
        )
    return CommandResult(selected, int(revision), str(state))
