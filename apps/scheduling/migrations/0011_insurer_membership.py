"""Constrain retained UUID values and add explicit immutable payer-check linkage."""

from typing import ClassVar

from django.db import migrations, models

from apps.insurance.migrations._scheduling_sql import SQL


class Migration(migrations.Migration):
    """Repeatable, non-destructive integration; invalid old references stop upgrade."""

    atomic = False
    dependencies: ClassVar = [
        ("scheduling", "0010_patient_service_booking"),
        ("intake", "0029_membership_payer"),
        ("insurance", "0001_revenue_cycle"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(
            SQL,
            state_operations=[
                # Only state changes its name; the existing UUID column stays.
                migrations.RemoveField("appointment", "payer_membership_id"),
                migrations.AddField(
                    "appointment",
                    "payer_membership",
                    models.ForeignKey(
                        "insurance.insurancemembership",
                        on_delete=models.PROTECT,
                        null=True,
                        blank=True,
                        related_name="appointments",
                    ),
                ),
                migrations.AddField(
                    "appointment",
                    "payer_contract",
                    models.ForeignKey(
                        "insurance.payercontract",
                        on_delete=models.PROTECT,
                        null=True,
                        blank=True,
                    ),
                ),
                migrations.AddField(
                    "appointment",
                    "eligibility_check",
                    models.ForeignKey(
                        "insurance.payerrequest",
                        on_delete=models.PROTECT,
                        null=True,
                        blank=True,
                        related_name="+",
                        editable=False,
                    ),
                ),
                migrations.AddField(
                    "appointment",
                    "authorization_check",
                    models.ForeignKey(
                        "insurance.payerrequest",
                        on_delete=models.PROTECT,
                        null=True,
                        blank=True,
                        related_name="+",
                        editable=False,
                    ),
                ),
            ],
        )
    ]
