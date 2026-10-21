"""Download exact source bytes after the current clinical permission check."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.ehr.attachment_storage import AttachmentStorageError
from apps.identity.otp import privileged_totp_required
from apps.orders.access import require_version
from apps.orders.models import ResultVersion
from apps.orders.storage import source_bytes
from apps.orders.values import OrdersAccessDeniedError, OrdersConflictError

if TYPE_CHECKING:
    from django.http import HttpRequest


@privileged_totp_required(
    lambda clinic_id: reverse("orders:results", kwargs={"clinic_id": clinic_id})
)
@require_POST
def download(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Materialize only a current, authorized, exactly matched source."""
    try:
        version_id = UUID(request.POST.get("version_id", ""))
        require_version(
            clinic_id=clinic_id, version_id=version_id, permission="result.read"
        )
        version = ResultVersion.objects.get(pk=version_id, clinic_id=clinic_id)
        data = source_bytes(version)
    except (
        ValueError,
        OrdersAccessDeniedError,
        OrdersConflictError,
        AttachmentStorageError,
        ValidationError,
    ):
        return HttpResponse(status=403)
    return HttpResponse(
        data,
        content_type=version.content_type,
        headers={
            "Content-Disposition": 'attachment; filename="resultado.pdf"',
            "Cache-Control": "no-store",
        },
    )
