"""Add immutable verification candidates; rollback requires restore."""

from typing import ClassVar

from django.db import migrations, models

from apps.prescription.migrations._independent_signature_sql import SQL


class Migration(migrations.Migration):
    """Keep existing artifacts intact and every live issuance gate closed."""

    dependencies: ClassVar = [("prescription", "0013_class_catalog_directions")]
    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="signatureoperation",
            name="prescription_signature_live_unique",
        ),
        migrations.RemoveConstraint(
            model_name="signatureoperation",
            name="prescription_signature_state",
        ),
        migrations.RemoveConstraint(
            model_name="signatureoperation",
            name="prescription_signature_operation_id_state",
        ),
        migrations.AlterField(
            model_name="signatureoperation",
            name="state",
            field=models.CharField(
                choices=[
                    ("draft", "Rascunho"),
                    ("prepared", "Preparado"),
                    ("signing", "Assinando"),
                    ("signed_unverified", "Assinado, aguardando verificação"),
                    ("issued", "Emitido"),
                    ("rehearsal_complete", "Ensaio concluído"),
                    ("failed", "Falhou"),
                ],
                default="draft",
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(
                    state__in=["draft", "prepared", "signing", "signed_unverified"]
                ),
                name="prescription_signature_live_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    state__in=[
                        "draft",
                        "prepared",
                        "signing",
                        "signed_unverified",
                        "issued",
                        "rehearsal_complete",
                        "failed",
                    ]
                ),
                name="prescription_signature_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=~models.Q(state="signed_unverified")
                | models.Q(
                    signed_bytes__isnull=False,
                    signed_digest__isnull=False,
                    completed_at__isnull=True,
                    failure_reason="",
                ),
                name="prescription_signature_unverified_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=[
                            "signing",
                            "signed_unverified",
                            "issued",
                            "rehearsal_complete",
                        ]
                    )
                    & ~models.Q(operation_id="")
                )
                | models.Q(state__in=["draft", "prepared", "failed"]),
                name="prescription_signature_operation_id_state",
            ),
        ),
        migrations.RunSQL(SQL),
    ]
