"""Conserve outgoing transfer lines across refund and payout instructions."""

from typing import ClassVar

from django.db import migrations, models

from apps.billing.migrations._transfer_confirmation_sql import SQL


class Migration(migrations.Migration):
    """No existing financial row is rewritten; a historical conflict stops upgrade."""

    dependencies: ClassVar = [("billing", "0014_merge_sandbox_psp_fiscal_documents")]
    operations: ClassVar = [
        migrations.RunSQL(
            SQL,
            state_operations=[
                migrations.CreateModel(
                    name="TransferConfirmation",
                    fields=[
                        ("evidence_reference", models.UUIDField()),
                        (
                            "entry",
                            models.OneToOneField(
                                to="billing.journalentry",
                                primary_key=True,
                                serialize=False,
                                on_delete=models.PROTECT,
                            ),
                        ),
                        (
                            "book",
                            models.ForeignKey(
                                to="billing.journalbook", on_delete=models.PROTECT
                            ),
                        ),
                        (
                            "organization",
                            models.ForeignKey(
                                to="identity.organization", on_delete=models.CASCADE
                            ),
                        ),
                    ],
                    options={
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("book", "evidence_reference"),
                                name="billing_transfer_line_uniq",
                            )
                        ]
                    },
                ),
            ],
        ),
    ]
