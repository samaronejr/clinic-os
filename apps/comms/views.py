"""Clinic-scoped pt-BR reminder receipts, without message or contact content."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.comms.adapters import OperationScope
from apps.comms.capabilities import CHANNELS, channel_capability
from apps.comms.models import AppointmentReminder
from apps.comms.services import reminder_send_eligible
from apps.identity.otp import privileged_totp_required
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    authorized_appointment_manager_clinic,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse

LABELS = {
    "pending": "Na fila",
    "in_progress": "Em processamento",
    "succeeded": "Enviado · aguardando confirmação",
    "delivered": "Entrega confirmada",
    "failed": "Falhou",
    "cancelled": "Cancelado · não será enviado",
    "blocked": "Bloqueado · canal não autorizado",
}


def reminders_continuation(clinic_id: UUID) -> str:
    """Resume only the clinic-scoped receipt list after TOTP."""
    return reverse("scheduling:reminders", args=(clinic_id,))


@privileged_totp_required(reminders_continuation)
@require_GET
def reminders_view(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Show current delivery facts and separately label synthetic acceptance."""
    try:
        clinic = authorized_appointment_manager_clinic(clinic_id)
    except AppointmentAccessDeniedError as error:
        raise Http404 from error
    reminders = (
        AppointmentReminder.objects.filter(operation__clinic_id=clinic_id)
        .select_related("operation", "appointment__patient")
        .order_by("-operation__created_at", "operation_id")[:100]
    )
    rows = []
    for reminder in reminders:
        operation = reminder.operation
        state = operation.status
        capability = channel_capability(operation.channel)
        if state in {"pending", "in_progress"}:
            scope = OperationScope(
                operation.pk, operation.organization_id, clinic_id, operation.actor_id
            )
            if not reminder_send_eligible(scope):
                state = "cancelled"
            elif not capability.synthetic_enabled:
                state = "blocked"
        rows.append(
            {
                "reminder": reminder,
                "operation": operation,
                "state": state,
                "label": LABELS[state],
                "synthetic": bool(
                    operation.provider_reference
                    and operation.provider_reference.startswith("synthetic:")
                ),
            }
        )
    return render(
        request,
        "comms/reminders.html",
        {
            "clinic": clinic,
            "rows": rows,
            "capabilities": [channel_capability(channel) for channel in CHANNELS],
        },
    )
