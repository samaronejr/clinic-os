"""Prepare patient-specific source forms without widening service authority."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.orders.access import require_ingest
from apps.orders.forms import UploadForm
from apps.orders.queries import result_rows, trends
from apps.orders.values import OrdersAccessDeniedError

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

    from apps.orders.models import Order
    from apps.orders.queries import ResultRow, TrendRow


def prepare_reviews(
    request: HttpRequest,
    clinic_id: UUID,
    orders: tuple[Order, ...],
    *,
    invalid_upload: bool,
) -> tuple[
    list[tuple[ResultRow, UploadForm | None]],
    list[tuple[str, tuple[TrendRow, ...]]],
]:
    """Keep each patient's review cards and trend series in their own scope."""
    result_forms: list[tuple[ResultRow, UploadForm | None]] = []
    trend_groups: list[tuple[str, tuple[TrendRow, ...]]] = []
    names = {
        order.enrollment_id: order.enrollment.patient.full_name for order in orders
    }
    for enrollment_id, patient_name in names.items():
        try:
            rows = result_rows(clinic_id=clinic_id, enrollment_id=enrollment_id)
            observations = trends(clinic_id=clinic_id, enrollment_id=enrollment_id)
        except OrdersAccessDeniedError:
            continue
        if observations:
            trend_groups.append((patient_name, observations))
        for row in rows:
            correction = None
            if row.can_upload:
                bound_correction = invalid_upload and request.POST.get(
                    "predecessor_id"
                ) == str(row.match.version_id)
                correction = UploadForm(
                    request.POST if bound_correction else None,
                    request.FILES if bound_correction else None,
                    initial={
                        "order_id": row.match.order_id,
                        "result_id": row.match.version.result_id,
                        "predecessor_id": row.match.version_id,
                        "event_id": row.correction_key,
                    },
                    auto_id=f"correction_{row.match.pk}_%s",
                )
                if correction.is_bound:
                    correction.is_valid()
            result_forms.append((row, correction))
    return result_forms, trend_groups


def prepare_uploads(
    request: HttpRequest,
    clinic_id: UUID,
    orders: tuple[Order, ...],
    *,
    invalid_upload: bool,
) -> list[tuple[Order, UploadForm]]:
    """Offer source forms only where the current actor may actually ingest."""
    upload_forms = []
    for order in orders:
        try:
            require_ingest(clinic_id=clinic_id, order_id=order.pk)
        except OrdersAccessDeniedError:
            continue
        bound = (
            invalid_upload
            and not request.POST.get("result_id")
            and (request.POST.get("order_id") == str(order.pk))
        )
        form = UploadForm(
            request.POST if bound else None,
            request.FILES if bound else None,
            initial={"order_id": order.pk, "event_id": uuid4()},
            auto_id=f"upload_{order.pk}_%s",
        )
        if bound:
            form.is_valid()
        upload_forms.append((order, form))
    return upload_forms
