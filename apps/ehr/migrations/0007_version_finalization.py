"""Add amendment lineage and finalization fields to document versions."""

from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Extend the version row; transition authority lands in 0008."""

    dependencies: ClassVar = [("ehr", "0006_attachment_policy")]

    operations: ClassVar = [
        migrations.AddField(
            model_name="clinicaldocumentversion",
            name="amendment_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="amendments",
                to="ehr.clinicaldocumentversion",
            ),
        ),
        migrations.AddField(
            model_name="clinicaldocumentversion",
            name="amendment_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="clinicaldocumentversion",
            name="content_digest",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="clinicaldocumentversion",
            name="finalized_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.CheckConstraint(
                condition=models.Q(content_digest="")
                | models.Q(content_digest__regex="^[0-9a-f]{64}$"),
                name="ehr_version_digest_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=["finalized", "superseded"],
                        finalized_at__isnull=False,
                    )
                    & ~models.Q(content_digest="")
                )
                | (
                    models.Q(
                        state__in=["draft", "discarded"],
                        finalized_at__isnull=True,
                    )
                    & models.Q(content_digest="")
                ),
                name="ehr_version_finalized_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.CheckConstraint(
                condition=(models.Q(amendment_of__isnull=True, amendment_reason=""))
                | (
                    models.Q(amendment_of__isnull=False)
                    & ~models.Q(amendment_reason="")
                    & models.Q(version__gte=2)
                ),
                name="ehr_version_amendment_check",
            ),
        ),
    ]
