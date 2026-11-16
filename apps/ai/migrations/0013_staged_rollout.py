"""Add reversible, clinic-bound stage evidence to the existing AI ledger."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _rollout_sql


class Migration(migrations.Migration):
    """No new tenant table, encrypted column, provider, or external approval."""

    dependencies: ClassVar = [("ai", "0012_approval_attestation")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="gatewayswitch",
            name="generation",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="evaluationrun",
            name="stage",
            field=models.CharField(default="offline", max_length=24),
        ),
        migrations.AddField(
            model_name="evaluationrun",
            name="rollout_approval",
            field=models.ForeignKey(
                "ai.modelchangerequest",
                null=True,
                on_delete=models.PROTECT,
                related_name="+",
            ),
        ),
        migrations.AddField(
            model_name="modelchangerequest",
            name="stage",
            field=models.CharField(default="shadow", max_length=24),
        ),
        migrations.AddField(
            model_name="modelchangerequest",
            name="exposure",
            field=models.CharField(default="none", max_length=16),
        ),
        migrations.AddField(
            model_name="modelchangerequest",
            name="switch_generation",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="modelchangerequest",
            name="authority_sha256",
            field=models.CharField(default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="modelchangerequest",
            name="status",
            field=models.CharField(default="requested", max_length=11),
        ),
        *[
            migrations.AddField(
                model_name=model,
                name="rollout_approval",
                field=models.ForeignKey(
                    "ai.modelchangerequest",
                    null=True,
                    on_delete=models.PROTECT,
                    related_name="+",
                ),
            )
            for model in ("aiinvocation", "agentrun")
        ],
        migrations.RunSQL(_rollout_sql.SQL, reverse_sql=_rollout_sql.REVERSE_SQL),
    ]
