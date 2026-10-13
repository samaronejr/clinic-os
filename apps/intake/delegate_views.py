"""Body-only representation switching and bounded renewal requests."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.core import signing
from django.db import connection
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.identity.delegates import (
    DelegateConflictError,
    request_renewal,
    switch_delegate,
)
from apps.intake.access import PatientAccessDeniedError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

RENEWAL_WINDOW: Final = timedelta(days=30)
OFFER_MAX_AGE: Final = 1800


def delegate_session_status(session_id: UUID) -> str | None:
    """Resolve a recovery state only for a still-live original patient identity."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.delegate_session_status(%s)", [session_id])
        row = cursor.fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def renewal_offer(session_id: UUID) -> str:
    """Sign a retry identity and deadline without exposing any patient selector."""
    return signing.dumps(
        {
            "key": str(uuid4()),
            "deadline": (timezone.now() + RENEWAL_WINDOW).isoformat(),
        },
        salt=f"delegate-renewal:{session_id}",
    )


@sensitive_post_parameters()
@require_POST
def switch_representation(request: HttpRequest) -> HttpResponseBase:
    """Switch solely by a stored grant; posted represented identities are ignored."""
    try:
        clinic_id = UUID(request.POST.get("clinic_id", ""))
        raw_grant = request.POST.get("grant_id", "")
        grant_id = UUID(raw_grant) if raw_grant else None
        switch_delegate(clinic_id=clinic_id, grant_id=grant_id)
    except (ValueError, PatientAccessDeniedError):
        return render(
            request, "intake/patient_gate.html", {"state": "required"}, status=403
        )
    return redirect("intake:patient-home")


@sensitive_post_parameters("offer")
@require_POST
def renew_representation(request: HttpRequest) -> HttpResponseBase:
    """Request staff re-verification of the same scope, never immediate access."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT session_id,clinic_id FROM clinic_app.delegate_recovery_identity()"
        )
        row = cursor.fetchone()
    if row is None:
        return render(
            request, "intake/patient_gate.html", {"state": "required"}, status=403
        )
    try:
        data = signing.loads(
            request.POST.get("offer", ""),
            salt=f"delegate-renewal:{row[0]}",
            max_age=OFFER_MAX_AGE,
        )
        request_renewal(
            clinic_id=row[1],
            idempotency_key=UUID(data["key"]),
            expires_at=datetime.fromisoformat(data["deadline"]),
        )
    except (
        signing.BadSignature,
        ValueError,
        KeyError,
        TypeError,
        PatientAccessDeniedError,
    ):
        return render(
            request, "intake/patient_gate.html", {"state": "required"}, status=403
        )
    except DelegateConflictError:
        return render(
            request, "intake/patient_gate.html", {"state": "required"}, status=409
        )
    return render(
        request, "intake/patient_gate.html", {"state": "renewal_requested"}, status=200
    )
