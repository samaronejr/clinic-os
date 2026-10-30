"""Patient portal results: released versions only, downloads by POST body."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.ehr.attachment_storage import AttachmentStorageError
from apps.orders.patient_results import patient_result_file, patient_results
from apps.orders.values import OrdersAccessDeniedError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

TEMPLATE = "orders/patient_results.html"


def _refused(request: HttpRequest) -> HttpResponse:
    return render(
        request, "intake/patient_gate.html", {"state": "required"}, status=403
    )


@never_cache
@require_http_methods(["GET", "POST"])
def patient_results_view(request: HttpRequest) -> HttpResponseBase:
    """List results the clinic released to this session and serve their bytes."""
    if request.method == "POST":
        if request.POST.get("action", "") != "download":
            return _refused(request)
        try:
            source = patient_result_file(
                version_id=UUID(request.POST.get("version_id", ""))
            )
        except (ValueError, OrdersAccessDeniedError):
            return _refused(request)
        except (ValidationError, AttachmentStorageError):
            return render(
                request,
                TEMPLATE,
                {
                    "results": patient_results(),
                    "error": gettext(
                        "This file is not available right now. Try again later or "
                        "ask the clinic team for a copy."
                    ),
                },
                status=503,
            )
        return HttpResponse(
            source.data,
            content_type=source.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{source.file_name}"',
                "Cache-Control": "no-store",
            },
        )
    try:
        results = patient_results()
    except OrdersAccessDeniedError:
        return _refused(request)
    return render(request, TEMPLATE, {"results": results})
