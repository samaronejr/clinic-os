"""Inbox feed for current results awaiting clinician acknowledgment.

Bulk acknowledgment is a narrow convenience over ``acknowledge_result``: only
current, unacknowledged, non-critical versions whose every source observation
is explicitly flagged ``normal`` qualify, each one is previewed before the
signed confirmation, and confirmation rechecks every condition under the
series fence, acknowledging all or none. Release is never bulk: it stays a
per-version decision in the orders workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.core import signing
from django.db import transaction
from django.db.models import BooleanField
from django.db.models.expressions import RawSQL
from django.db.models.signals import post_save
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.orders.access import current_version
from apps.orders.models import (
    ResultAcknowledgment,
    ResultMatch,
    ResultObservation,
    ResultVersion,
)
from apps.orders.review import acknowledge_result
from apps.orders.values import CATALOG, OrdersConflictError
from apps.workflows.inbox import (
    InboxInputError,
    InboxItem,
    InboxObservation,
    publish_inbox_hint,
    register_inbox_feed,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apps.workflows.inbox import InboxScope

MAX_BULK: Final = 50
BULK_SALT: Final = "orders-inbox-bulk-acknowledgment-v1"
BULK_MAX_AGE: Final = 900
BULK_DISPOSITION: Final = "no_action"


def _may_acknowledge(clinic_id: UUID, enrollment_id: UUID) -> bool:
    try:
        require_permission(
            "result.acknowledge",
            clinic_id=clinic_id,
            patient_enrollment_id=enrollment_id,
        )
    except CurrentActorError:
        return False
    return True


def _pending(scope: InboxScope) -> Iterable[ResultMatch]:
    pending = (
        ResultMatch.objects.filter(
            clinic_id=scope.clinic_id, version__resultacknowledgment__isnull=True
        )
        .annotate(
            current=RawSQL(
                "clinic_app.orders_is_current(orders_resultmatch.version_id)",
                (),
                output_field=BooleanField(),
            )
        )
        .filter(current=True)
    )
    # Authorize per enrollment before bounding, so the bound never hides an
    # acknowledgeable result behind rows the actor may not act on.
    enrollments = set(
        pending.order_by().values_list("order__enrollment_id", flat=True).distinct()
    )
    permitted = [
        enrollment
        for enrollment in sorted(enrollments)
        if _may_acknowledge(scope.clinic_id, enrollment)
    ]
    return (
        pending.filter(order__enrollment_id__in=permitted)
        .select_related("version", "order__enrollment__patient")
        .prefetch_related("order__items", "version__observations")
        .order_by("-version__critical", "version__created_at", "pk")[: scope.limit]
    )


def _observations(version: ResultVersion) -> tuple[InboxObservation, ...]:
    rows: Iterable[ResultObservation] = sorted(
        version.observations.all(), key=lambda row: row.position
    )
    return tuple(
        InboxObservation(
            code=row.source["code"],
            label=str(CATALOG.get(row.source["code"], row.source["code"])),
            value=row.source["value"],
            unit=row.source["unit"],
            reference_interval=row.source["reference_interval"],
            flag=row.source["abnormal_flag"],
        )
        for row in rows
    )


def bulk_eligible(
    version: ResultVersion, observations: tuple[InboxObservation, ...]
) -> bool:
    """Only explicitly normal, non-critical sources qualify; absence is not normal."""
    return (
        not version.critical
        and bool(observations)
        and all(row.flag == "normal" for row in observations)
    )


def result_items(scope: InboxScope) -> tuple[InboxItem, ...]:
    """Return current unacknowledged results the actor may acknowledge.

    Result RLS also admits identity-routing staff, so the feed rechecks the
    patient-scoped ``result.acknowledge`` permission (professional
    registration and care-team scope) for every enrollment it shows.
    Critical results come first so the page bound never hides them.
    """
    items = []
    for match in _pending(scope):
        version = match.version
        observations = _observations(version)
        exams = ", ".join(
            str(CATALOG[item.exam_code])
            for item in sorted(match.order.items.all(), key=lambda row: row.exam_code)
        )
        items.append(
            InboxItem(
                kind="result",
                record_id=version.pk,
                target_id=version.pk,
                revision=version.number,
                title=_("Result to review: %(exams)s") % {"exams": exams},
                patient_label=match.order.enrollment.patient.full_name,
                owner="me" if match.order.ordered_by_id == scope.actor_id else "team",
                due_at=version.created_at,
                priority="urgent" if version.critical else "normal",
                critical=version.critical,
                action="acknowledge",
                bulk_eligible=bulk_eligible(version, observations),
                preview=observations,
            )
        )
    return tuple(items)


@dataclass(frozen=True, slots=True)
class BulkPreview:
    """The exact items a confirmation would acknowledge, and its signed token."""

    items: tuple[InboxItem, ...]
    token: str


def preview_bulk_acknowledgment(
    *, clinic_id: UUID, items: tuple[InboxItem, ...], version_ids: tuple[UUID, ...]
) -> BulkPreview:
    """Bind the actor, clinic and exact version numbers of eligible selections."""
    selected = tuple(dict.fromkeys(version_ids))
    if not selected or len(selected) > MAX_BULK:
        raise InboxInputError
    by_version = {
        item.record_id: item
        for item in items
        if item.kind == "result" and item.bulk_eligible
    }
    if any(version not in by_version for version in selected):
        raise OrdersConflictError(code="bulk_ineligible")
    chosen = tuple(by_version[version] for version in selected)
    token = signing.dumps(
        {
            "clinic": str(clinic_id),
            "actor": str(current_actor_id()),
            "versions": sorted([str(item.record_id), item.revision] for item in chosen),
        },
        salt=BULK_SALT,
    )
    return BulkPreview(items=chosen, token=token)


def _still_eligible(clinic_id: UUID, version_id: UUID, number: int) -> bool:
    version = current_version(clinic_id=clinic_id, version_id=version_id)
    match = (
        ResultMatch.objects.filter(version_id=version_id, clinic_id=clinic_id)
        .select_related("order")
        .first()
    )
    return (
        match is not None
        and version.number == number
        and not ResultAcknowledgment.objects.filter(version_id=version_id).exists()
        and _may_acknowledge(clinic_id, match.order.enrollment_id)
        and bulk_eligible(version, _observations(version))
    )


@transaction.atomic
def apply_bulk_acknowledgment(
    *, clinic_id: UUID, token: str
) -> tuple[ResultAcknowledgment, ...]:
    """Acknowledge every previewed version as ``no_action``, or none of them."""
    try:
        terms = signing.loads(token, salt=BULK_SALT, max_age=BULK_MAX_AGE)
        versions = [(UUID(row[0]), int(row[1])) for row in terms["versions"]]
        bound = (terms["clinic"], terms["actor"])
    except (signing.BadSignature, KeyError, TypeError, ValueError) as error:
        raise OrdersConflictError(code="bulk_preview_stale") from error
    if bound != (str(clinic_id), str(current_actor_id())) or not (
        0 < len(versions) <= MAX_BULK
    ):
        raise OrdersConflictError(code="bulk_preview_stale")
    receipts = []
    for version_id, number in versions:
        if not _still_eligible(clinic_id, version_id, number):
            raise OrdersConflictError(code="bulk_preview_stale")
        receipts.append(
            acknowledge_result(
                clinic_id=clinic_id,
                version_id=version_id,
                disposition=BULK_DISPOSITION,
            )
        )
    return tuple(receipts)


def result_changed(
    sender: type[ResultVersion | ResultAcknowledgment],
    instance: ResultVersion | ResultAcknowledgment,
    **kwargs: object,
) -> None:
    """Hint subscribed inboxes after a new source or acknowledgment commits."""
    del sender, kwargs
    publish_inbox_hint(instance.clinic_id)


def register() -> None:
    """Register the result feed and its commit-bound refetch hints."""
    register_inbox_feed(
        "result", result_items, label=gettext_noop("Results to review"), position=10
    )
    for model in (ResultVersion, ResultAcknowledgment):
        post_save.connect(
            result_changed,
            sender=model,
            dispatch_uid=f"orders.inbox.{model.__name__}",
        )
