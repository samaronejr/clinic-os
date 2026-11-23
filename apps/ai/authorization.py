"""One purpose-dispatched authority boundary for protected AI inputs and turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, assert_never, overload
from uuid import UUID

from django.conf import settings
from django.db import connection

from apps.ai.brief_contracts import parse_request
from apps.ai.chart_authority import recheck_chart_permission
from apps.ai.contracts import (
    GatewayAccessDeniedError,
    GatewayRefusedError,
    Purpose,
    request_schema,
)
from apps.ai.decision_policy import authorize_decision
from apps.ai.pending_choice import pending_choice_allows
from apps.ai.revenue_sources import recheck_pending_context as recheck_revenue
from apps.ai.revenue_sources import require_finance
from apps.analytics.analyst_contracts import recheck_request as recheck_analyst
from apps.analytics.contracts import SCHEMA as ANALYST_SCHEMA
from apps.core.secrets import secret_store
from apps.identity.current_context import require_permission
from apps.tenancy.envelope import KEK_SECRET_NAME

if TYPE_CHECKING:
    from datetime import datetime

    from apps.ai.brief_contracts import ChartDisplay


@dataclass(frozen=True, slots=True)
class StaffInput:
    """Scope and immutable input material; the database supplies actor identity."""

    clinic_id: UUID
    enrollment_id: UUID | None
    content: str = field(repr=False)
    display: ChartDisplay | None = None
    generated_at: datetime | None = None
    retained_input: UUID | None = None


@dataclass(frozen=True, slots=True)
class MessageSubject:
    """No staff impersonation: the SQL bridge checks the current channel/session."""

    subject_id: UUID
    kind: Literal["message", "turn", "completed_turn"]
    receipt: bytes = field(repr=False)


def _authorize_message(subject: MessageSubject) -> UUID | str | None:
    """Use SQL to authenticate the channel/session, source and rollout atomically."""
    enqueue = subject.kind == "message"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_front_desk_enqueue(%s,%s,%s)"
            if enqueue
            else "SELECT clinic_app.ai_front_desk_authorize(%s,%s,%s)",
            [
                subject.subject_id,
                subject.receipt,
                secret_store().get_secret(KEK_SECRET_NAME),
            ],
        )
        row = cursor.fetchone()
    if enqueue:
        return row[0] if row and isinstance(row[0], UUID) else None
    return str(row[0]) if row and row[0] else ""


@overload
def authorize_capability(purpose: Purpose, subject: StaffInput) -> UUID: ...


@overload
def authorize_capability(
    purpose: Purpose, subject: MessageSubject
) -> UUID | str | None: ...


def authorize_capability(
    purpose: Purpose, subject: StaffInput | MessageSubject
) -> UUID | str | None:
    """Decide from purpose and trusted scope, never an optional payload schema."""
    if isinstance(subject, MessageSubject):
        if purpose is not Purpose.FRONT_DESK:
            raise GatewayAccessDeniedError
        if settings.CLINIC_DATA_MODE != "synthetic":
            return None
        # The turn and message resolvers authenticate the caller and the exact
        # source/rollout before recording anything, even without a staff GUC.
        return _authorize_message(subject)
    return _authorize_staff_input(purpose, subject)


def _authorize_staff_input(purpose: Purpose, subject: StaffInput) -> UUID:
    clinic, enrollment = subject.clinic_id, subject.enrollment_id
    match purpose:
        case Purpose.FRONT_DESK:
            actor = require_permission(
                "appointment.read", clinic_id=clinic, patient_enrollment_id=enrollment
            )
            _pending_choice(subject, patient_optional=True)
            return actor
        case Purpose.ANALYST:
            actor = require_permission(
                "charge.read", clinic_id=clinic, patient_enrollment_id=enrollment
            )
            if request_schema(subject.content) == ANALYST_SCHEMA:
                recheck_analyst(clinic_id=clinic, content=subject.content)
            return actor
        case Purpose.REVENUE_CYCLE:
            if enrollment is not None:
                raise GatewayAccessDeniedError
            finance_actor = require_finance(clinic_id=clinic)
            recheck_revenue(
                clinic_id=clinic,
                content=subject.content,
                retained_input=subject.retained_input,
            )
            return finance_actor
        case (
            Purpose.SCRIBE_ASR
            | Purpose.SCRIBE_DRAFT
            | Purpose.BRIEF
            | Purpose.EXTRACTION
            | Purpose.DECISION_SUPPORT
            | Purpose.CARE_COORDINATION
        ):
            actor = require_permission(
                "clinical.read", clinic_id=clinic, patient_enrollment_id=enrollment
            )
            if purpose is Purpose.BRIEF:
                recheck_chart_permission(
                    clinic_id=clinic,
                    enrollment_id=enrollment,
                    request=parse_request(subject.content),
                    display=subject.display,
                )
            elif purpose is Purpose.DECISION_SUPPORT:
                authorize_decision(
                    clinic_id=clinic,
                    content=subject.content,
                    enrollment_id=enrollment,
                    display=subject.display,
                )
            elif purpose in (Purpose.CARE_COORDINATION, Purpose.EXTRACTION):
                _pending_choice(subject)
            return actor
        case unreachable:
            assert_never(unreachable)


def _pending_choice(subject: StaffInput, *, patient_optional: bool = False) -> None:
    if patient_optional and subject.enrollment_id is None:
        return
    if subject.enrollment_id is None or not pending_choice_allows(
        clinic_id=subject.clinic_id,
        enrollment_id=subject.enrollment_id,
        generated_at=subject.generated_at,
    ):
        raise GatewayRefusedError(reason="authority_revoked")
