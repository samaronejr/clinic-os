"""Native-form document review with inert page rasters and exact region anchors."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from PIL import Image

from apps.ai.contracts import (
    Budget,
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    Usage,
)
from apps.ai.extraction import (
    authorized_job,
    candidate_data,
    collect_extraction,
    extraction_source,
    review_extraction,
    start_extraction,
)
from apps.ai.extraction_contracts import ExtractionInputError
from apps.ai.extraction_documents import render_page
from apps.ai.models import AIInvocation, ExtractionCandidateRecord
from apps.ehr.attachment_storage import AttachmentStorageError
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.otp import privileged_totp_required
from apps.orders.models import ResultMatch
from apps.orders.values import OrdersAccessDeniedError, OrdersConflictError

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.ai.models import ExtractionJob

ZOOMS = (100, 200, 400)


@privileged_totp_required(lambda clinic_id: reverse("ai:extraction", args=(clinic_id,)))
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def extraction_view(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Keep selections in POST bodies and every refusal inside a rollback boundary."""
    try:
        return _workspace(request, clinic_id)
    except (
        CurrentActorError,
        GatewayAccessDeniedError,
        ClinicalAccessDeniedError,
        OrdersAccessDeniedError,
    ):
        return render(request, "403.html", status=403)
    except (
        ValueError,
        ValidationError,
        GatewayInputError,
        GatewayRefusedError,
        ExtractionInputError,
        AttachmentStorageError,
        OrdersConflictError,
    ):
        return render(
            request,
            "ai/extraction.html",
            {
                "clinic_id": clinic_id,
                "error": _(
                    "Extraction unavailable or source changed. "
                    "Continue with manual result entry."
                ),
            },
            status=409,
        )


@transaction.atomic
def _workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    context: dict[str, object] = {"clinic_id": clinic_id}
    if request.method == "GET":
        require_permission("clinical.read", clinic_id=clinic_id)
        return render(request, "ai/extraction.html", context)
    if (
        request.FILES
        or set(request.POST)
        - {
            "csrfmiddlewaretoken",
            "action",
            "job_id",
            "candidate_id",
            "result_version_id",
            "attachment_id",
            "page",
            "zoom",
        }
        or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
    ):
        return HttpResponse(status=400)
    action = request.POST.get("action", "")
    target = request.POST.get("result_version_id", "")
    if action == "start":
        attachment = request.POST.get("attachment_id", "")
        job = start_extraction(
            clinic_id=clinic_id,
            result_version_id=UUID(target) if target else None,
            attachment_id=UUID(attachment) if attachment else None,
            budget=Budget(Usage(tokens_in=10000, tokens_out=10000), 1_000_000),
        )
    else:
        job = authorized_job(
            clinic_id=clinic_id, job_id=UUID(request.POST.get("job_id", ""))
        )
        if action in ("confirmed", "rejected"):
            candidate_id = UUID(request.POST.get("candidate_id", ""))
            if not ExtractionCandidateRecord.objects.filter(
                pk=candidate_id, job=job
            ).exists():
                raise GatewayAccessDeniedError
            review_extraction(
                clinic_id=clinic_id,
                candidate_id=candidate_id,
                decision=action,
                result_version_id=UUID(target) if target else None,
            )
            context["saved"] = True
        elif action != "refresh":
            return HttpResponse(status=400)
        job = collect_extraction(clinic_id=clinic_id, job_id=job.pk)
    context.update(
        _document_context(
            job,
            page=int(request.POST.get("page", "1")),
            zoom=int(request.POST.get("zoom", "100")),
        )
    )
    response = render(request, "ai/extraction.html", context)
    response["Cache-Control"] = "private, no-store"
    return response


def _document_context(job: ExtractionJob, *, page: int, zoom: int) -> dict[str, object]:
    if zoom not in ZOOMS:
        raise ExtractionInputError
    source = extraction_source(
        clinic_id=job.clinic_id,
        result_version_id=job.result_version_id,
        attachment_id=job.attachment_id,
        include_bytes=True,
    )
    raster = render_page(data=source.data, content_type=source.content_type, page=page)
    with Image.open(BytesIO(raster)) as image:
        width, height = image.size
    rows: list[dict[str, object]] = []
    for record in job.candidates.select_related("job").order_by("position"):
        if (
            record.status in ("candidate", "ambiguous")
            and record.expires_at <= timezone.now()
        ):
            continue
        candidate = candidate_data(record)
        box = candidate.region.display_bbox(job.rotations[candidate.region.page - 1])
        left, top, right, bottom = box
        rows.append(
            {
                "record": record,
                "candidate": candidate,
                "on_page": candidate.region.page == page,
                "x": round(left / 10000 * width),
                "y": round(top / 10000 * height),
                "width": round((right - left) / 10000 * width),
                "height": round((bottom - top) / 10000 * height),
            }
        )
    targets = ResultMatch.objects.filter(
        clinic_id=job.clinic_id,
        order__enrollment_id=job.patient_enrollment_id,
        version__sha256=job.sha256,
        version__successor__isnull=True,
    ).select_related("version")
    invocation = AIInvocation.objects.filter(pk=job.invocation_id).first()
    return {
        "job": job,
        "rows": rows,
        "page": page,
        "zoom": zoom,
        "zooms": ZOOMS,
        "pages": tuple(range(1, len(job.rotations) + 1)),
        "page_image": "data:image/png;base64,"
        + base64.b64encode(raster).decode("ascii"),
        "image_width": width,
        "image_height": height,
        "targets": targets,
        "unavailable": not rows
        and (invocation is None or invocation.status == "failed"),
    }
