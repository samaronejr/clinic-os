"""Authorization precedes chart selection, trend values and aggregate counts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connection
from django.db.models import BooleanField
from django.db.models.expressions import RawSQL

from apps.identity.current_context import CurrentActorError, require_permission
from apps.orders.access import require_ingest
from apps.orders.models import ResultAcknowledgment, ResultMatch, ResultRelease
from apps.orders.values import OrdersAccessDeniedError

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One current filed source; historical receipts cannot label it reviewed."""

    match: ResultMatch
    acknowledged: bool
    released: bool
    correction_key: UUID = field(default_factory=uuid4)
    can_acknowledge: bool = False
    can_release: bool = False
    can_upload: bool = False
    patient_released: bool = False


@dataclass(frozen=True, slots=True)
class TrendRow:
    """A table is the exact-source alternative to guessing numeric conversions."""

    recorded_at: datetime
    code: str
    value: str
    unit: str
    reference_interval: str
    abnormal_flag: str


def result_rows(*, clinic_id: UUID, enrollment_id: UUID) -> tuple[ResultRow, ...]:
    """No unauthorized caller learns whether the patient has results."""
    try:
        require_permission(
            "result.read", clinic_id=clinic_id, patient_enrollment_id=enrollment_id
        )
    except CurrentActorError as error:
        raise OrdersAccessDeniedError from error
    matched = (
        ResultMatch.objects.filter(
            clinic_id=clinic_id, order__enrollment_id=enrollment_id
        )
        .annotate(
            current=RawSQL(
                "clinic_app.orders_is_current(orders_resultmatch.version_id)",
                (),
                output_field=BooleanField(),
            )
        )
        .filter(current=True)
        .select_related("version", "order__enrollment__patient")
        .order_by("-version__created_at")[:200]
    )
    rows = []
    for row in matched:
        allowed: set[str] = set()
        for permission in ("result.acknowledge", "result.release"):
            try:
                require_permission(
                    permission,
                    clinic_id=clinic_id,
                    patient_enrollment_id=enrollment_id,
                )
            except CurrentActorError:
                continue
            allowed.add(permission)
        can_upload = False
        try:
            require_ingest(clinic_id=clinic_id, order_id=row.order_id)
        except OrdersAccessDeniedError:
            pass
        else:
            can_upload = True
        rows.append(
            ResultRow(
                row,
                ResultAcknowledgment.objects.filter(version_id=row.version_id).exists(),
                ResultRelease.objects.filter(version_id=row.version_id).exists(),
                can_acknowledge="result.acknowledge" in allowed,
                can_release="result.release" in allowed,
                can_upload=can_upload,
                patient_released=ResultRelease.objects.filter(
                    version_id=row.version_id, audience="patient"
                ).exists(),
            )
        )
    return tuple(rows)


def trends(*, clinic_id: UUID, enrollment_id: UUID) -> tuple[TrendRow, ...]:
    """Keep source decimal separators, units and flags; never infer normal ranges."""
    rows = result_rows(clinic_id=clinic_id, enrollment_id=enrollment_id)
    return tuple(
        TrendRow(
            row.match.version.created_at,
            observation.source["code"],
            observation.source["value"],
            observation.source["unit"],
            observation.source["reference_interval"],
            observation.source["abnormal_flag"],
        )
        for row in reversed(rows)
        for observation in row.match.version.observations.order_by("position")
    )


@dataclass(frozen=True, slots=True)
class PatientSource:
    """Only an audience-authorized source descriptor can leave the resolver."""

    version_id: UUID
    number: int
    created_at: datetime
    storage_key: str
    sha256: str
    size_bytes: int
    content_type: str


def patient_sources(*, version_id: UUID | None = None) -> tuple[PatientSource, ...]:
    """One resolver rechecks session, delegate, release, version and acknowledgment."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.orders_patient_sources(%s)", [version_id]
        )
        return tuple(PatientSource(*row) for row in cursor.fetchall())
