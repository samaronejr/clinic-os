"""Keep v1 availability grants fixed; opt-in v2 grants authorize bounded agents."""

from typing import ClassVar

from django.db import migrations, models

from apps.identity.migrations._agent_authority_sql import SQL


class Migration(migrations.Migration):
    """Add v2 without promoting or widening any existing principal."""

    dependencies: ClassVar = [("identity", "0039_ai_policy")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="serviceprincipalgrant",
            name="grant_set_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RemoveConstraint(
            model_name="serviceprincipal",
            name="identity_principal_v1_identity",
        ),
        migrations.AddConstraint(
            model_name="serviceprincipal",
            constraint=models.CheckConstraint(
                condition=models.Q(grant_set_version__in=(1, 2))
                & models.Q(db_identity__regex=r"^clinic_agent(_[a-z0-9_]+)?$")
                & models.Q(name__regex=r"^[a-z0-9][a-z0-9_-]{0,63}$")
                & models.Q(purpose__regex=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
                name="identity_principal_v1_identity",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="serviceprincipalgrant",
            name="identity_principal_grant_v1_scope",
        ),
        migrations.AddConstraint(
            model_name="serviceprincipalgrant",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(grant_set_version=1, permission="appointment.read")
                    | models.Q(grant_set_version=2, permission="agent.execute")
                )
                & models.Q(subject_scope="clinic"),
                name="identity_principal_grant_v1_scope",
            ),
        ),
        migrations.RunSQL(SQL),
    ]
