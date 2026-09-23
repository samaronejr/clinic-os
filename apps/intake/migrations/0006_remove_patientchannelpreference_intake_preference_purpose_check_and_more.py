"""Require separate explicit consent for outbound waitlist-offer notices."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Extend the preference purpose without upgrading existing consent."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0005_clinic_timezone"),
        ("intake", "0005_booking_operation"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="patientchannelpreference",
            name="intake_preference_purpose_check",
        ),
        migrations.AlterField(
            model_name="patientchannelpreference",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("appointment_reminder", "Appointment reminder"),
                    ("booking_confirmation", "Booking confirmation"),
                    ("waitlist_offer", "Oferta da lista de espera"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="patientchannelpreference",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "purpose__in",
                        [
                            "appointment_reminder",
                            "booking_confirmation",
                            "waitlist_offer",
                        ],
                    )
                ),
                name="intake_preference_purpose_check",
            ),
        ),
    ]
