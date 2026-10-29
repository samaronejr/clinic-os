"""Browser QA bridge for holds and confirmation through real runtime services."""

from __future__ import annotations

import json
import sys
from uuid import UUID, uuid4

from apps.scheduling.models import Appointment
from apps.scheduling.services import AppointmentLocalRange, book, create_hold
from apps.tenancy.db import tenant_context
from django.db import connection


def main() -> None:
    """Run the service-only steps absent from the native agenda surface."""
    action, organization, clinic, actor, subject, day = sys.argv[1:]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
    with tenant_context(UUID(actor), UUID(organization)):
        if action == "hold":
            appointment = create_hold(
                clinic_id=UUID(clinic),
                enrollment_id=UUID(subject),
                practitioner_id=UUID(actor),
                local_range=AppointmentLocalRange(f"{day}T09:00", f"{day}T09:30"),
                idempotency_key=uuid4(),
            )
        else:
            assert action == "book"
            held = Appointment.objects.get(pk=UUID(subject), clinic_id=UUID(clinic))
            appointment = book(
                clinic_id=UUID(clinic),
                appointment_id=held.pk,
                expected_revision=held.revision,
                command_id=uuid4(),
            )
    sys.stdout.write(
        json.dumps(
            {
                "runtime_role": "clinic_app",
                "appointment_id": str(appointment.pk),
                "status": appointment.status,
                "synthetic": True,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
