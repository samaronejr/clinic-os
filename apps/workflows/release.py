"""Result release to the patient with clinician-approved wording (todo 55).

``release_result_to_patient`` composes the orders release, whose rules are
unchanged (the version's own acknowledgment; the patient or an active, correctly
scoped delegate grant), with one clinic-wide notice a clinician approved. The
notice is queued on the patient's portal care thread and never carries result
content. Its delivered state is the portal message's own outbox state. At send
time the outbox recheck asks ``workflows_release_send_eligible`` whether the
version is still current and, for a delegate, whether the grant still allows it,
so a delegate revoked or expired after the release receives nothing. When the
operation fails or is cancelled, the database outcome bridge marks the delivery
failed and opens one follow-up task for reception (see ``_coordination_sql``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid5

from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.comms.inbox_services import (
    SUBJECT_TYPE,
    InboxAccessDeniedError,
    InboxRefusedError,
    open_conversation,
    send_message,
)
from apps.comms.models import IntegrationOperation
from apps.identity.current_context import require_permission
from apps.identity.models import Clinic
from apps.orders.models import ResultMatch
from apps.orders.review import release_result
from apps.orders.values import OrdersAccessDeniedError, OrdersConflictError
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.release_models import (
    MAX_NOTICE_CHARS,
    ReleaseDelivery,
    ReleaseNoticeTemplate,
)
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError

if TYPE_CHECKING:
    from apps.comms.models import Message

__all__ = (
    "DeliveryView",
    "current_notice",
    "notice_send_eligible",
    "publish_release_notice",
    "release_deliveries",
    "release_result_to_patient",
)

# A notice is the same sentence for every patient: no digit or address can
# smuggle a date, a value, a phone number or an e-mail into it.
_IDENTIFYING: Final = re.compile(r"[0-9@]")
_NOTICE_NAMESPACE: Final = UUID("6f7b5c2e-1a4d-4c8e-9a61-55a0c0f2d055")
# result.release is patient-scoped: it needs an enrollment, so no clinic-level
# check can grant it. The clinic-level clinician gate is the physician bundle's
# configuration.propose; each release still needs patient-scoped result.release.
CLINIC_GATE: Final = "configuration.propose"


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """One release notice and its derived delivery state; no content."""

    delivery_id: UUID
    audience: str
    template_version: int
    state: Literal["pending", "delivered", "failed"]


def current_notice(*, clinic_id: UUID) -> ReleaseNoticeTemplate | None:
    """Return the clinic's latest approved notice, the only one a release binds."""
    require_permission(CLINIC_GATE, clinic_id=clinic_id)
    return (
        ReleaseNoticeTemplate.objects.filter(clinic_id=clinic_id)
        .order_by("-version")
        .first()
    )


@transaction.atomic
def publish_release_notice(*, clinic_id: UUID, body: str) -> ReleaseNoticeTemplate:
    """Approve the next notice version; the approving clinician is its author."""
    actor = require_permission(CLINIC_GATE, clinic_id=clinic_id)
    text = " ".join(body.split()) if type(body) is str else ""
    if not 0 < len(text) <= MAX_NOTICE_CHARS or _IDENTIFYING.search(text):
        raise WorkflowInputError
    clinic = Clinic.objects.get(pk=clinic_id)
    # Versions are append-only rows (no UPDATE grant, so no row lock): one
    # publication per clinic at a time decides the next version.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"workflows-release-notice:{clinic_id}"],
        )
    latest = (
        ReleaseNoticeTemplate.objects.filter(clinic_id=clinic_id)
        .order_by("-version")
        .first()
    )
    notice = ReleaseNoticeTemplate.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        version=latest.version + 1 if latest else 1,
        body=text,
        approved_by_id=actor,
    )
    record_phase1_event(
        "workflows.release_notice.approved",
        clinic_id=clinic_id,
        affected_record_id=notice.pk,
    )
    return notice


def _state(delivery: ReleaseDelivery) -> Literal["pending", "delivered", "failed"]:
    if delivery.failure_reason:
        return "failed"
    if delivery.message_id is None:
        return "pending"
    latest = (
        IntegrationOperation.objects.filter(
            subject_type=SUBJECT_TYPE, subject_id=delivery.message_id
        )
        .order_by("-created_at")
        .values_list("status", flat=True)
        .first()
    )
    if latest in {"succeeded", "delivered"}:
        return "delivered"
    if latest in {"failed", "cancelled"}:
        return "failed"
    return "pending"


def _view(delivery: ReleaseDelivery) -> DeliveryView:
    return DeliveryView(
        delivery.pk, delivery.audience, delivery.template.version, _state(delivery)
    )


@transaction.atomic
def release_result_to_patient(
    *,
    clinic_id: UUID,
    result_version_id: UUID,
    template_version: int,
    audience: str,
    delegate_grant_id: UUID | None = None,
) -> DeliveryView:
    """Release one acknowledged version and queue the approved portal notice.

    ``template_version`` must be the clinic's current notice version, so the
    clinician releases with the wording they saw. A repeated release returns
    the original delivery; nothing is sent twice.
    """
    require_permission(CLINIC_GATE, clinic_id=clinic_id)
    if type(template_version) is not int:
        raise WorkflowConflictError
    match = ResultMatch.objects.filter(
        version_id=result_version_id, clinic_id=clinic_id
    ).first()
    if match is None:
        raise WorkflowAccessDeniedError
    enrollment = match.order.enrollment_id
    # Decide the patient-scoped authority of every step before any write: the
    # release itself and the care thread that carries the notice.
    require_permission(
        "result.release", clinic_id=clinic_id, patient_enrollment_id=enrollment
    )
    require_permission(
        "clinical.read", clinic_id=clinic_id, patient_enrollment_id=enrollment
    )
    notice = current_notice(clinic_id=clinic_id)
    if notice is None or notice.version != template_version:
        raise WorkflowConflictError
    try:
        release = release_result(
            clinic_id=clinic_id,
            version_id=result_version_id,
            audience=audience,
            delegate_grant_id=delegate_grant_id,
        )
    except OrdersAccessDeniedError as error:
        raise WorkflowAccessDeniedError from error
    except OrdersConflictError as error:
        raise WorkflowConflictError from error
    existing = (
        ReleaseDelivery.objects.select_related("template")
        .filter(release=release)
        .first()
    )
    if existing is not None:
        return _view(existing)
    try:
        thread = open_conversation(
            clinic_id=clinic_id,
            enrollment_id=enrollment,
            channel="portal",
            purpose="care",
        )
        receipt = send_message(
            clinic_id=clinic_id,
            conversation_id=thread.conversation_id,
            command_id=uuid5(_NOTICE_NAMESPACE, str(release.pk)),
            body=notice.body,
        )
    except InboxAccessDeniedError as error:
        raise WorkflowAccessDeniedError from error
    except InboxRefusedError as error:
        raise WorkflowConflictError from error
    delivery = ReleaseDelivery.objects.create(
        organization_id=release.organization_id,
        clinic_id=clinic_id,
        release=release,
        version_id=result_version_id,
        enrollment_id=enrollment,
        audience=release.audience,
        delegate_grant_id=release.delegate_grant_id,
        template=notice,
        message_id=receipt.message_id,
        released_by_id=release.released_by_id,
    )
    record_phase1_event(
        "workflows.release_delivery.queued",
        clinic_id=clinic_id,
        affected_record_id=delivery.pk,
    )
    return _view(delivery)


def release_deliveries(
    *, clinic_id: UUID, result_version_id: UUID
) -> tuple[DeliveryView, ...]:
    """Return the notices of one version the releasing clinician may see."""
    require_permission(CLINIC_GATE, clinic_id=clinic_id)
    return tuple(
        _view(delivery)
        for delivery in ReleaseDelivery.objects.select_related("template")
        .filter(clinic_id=clinic_id, version_id=result_version_id)
        .order_by("created_at", "pk")
    )


def notice_send_eligible(message: Message) -> bool:
    """Send-time recheck of a release notice; other messages are not ours."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_release_send_eligible(%s)", [message.pk]
        )
        row = cursor.fetchone()
    return row is not None and row[0] is True
