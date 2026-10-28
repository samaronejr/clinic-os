"""Separate v3 retention authority and synthetic clinic policy defaults."""

from typing import ClassVar

from django.db import migrations, models

from apps.identity.migrations._principal_sql import SQL as PRINCIPAL_SQL

_start = PRINCIPAL_SQL.index("CREATE FUNCTION clinic_app.principal_scope(")
_end = PRINCIPAL_SQL.index("$f$;", _start) + len("$f$;")
SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + PRINCIPAL_SQL[_start:_end]
    .replace("clinic_app.principal_scope", "clinic_app.retention_principal_scope")
    .replace("p.grant_set_version=1", "p.grant_set_version=3 AND p.purpose='retention'")
    .replace(
        "g.permission='appointment.read'",
        "g.permission='retention.execute' AND g.grant_set_version=3",
    )
    + """
REVOKE ALL ON FUNCTION clinic_app.retention_principal_scope(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_principal_scope(uuid,uuid)
 TO clinic_agent;
RESET ROLE;
"""
)


class Migration(migrations.Migration):
    """Existing machine identities and grants remain immutable."""

    dependencies: ClassVar = [("identity", "0040_agent_execution_authority")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="clinicconfiguration",
            name="raw_audio_retention_days",
            field=models.PositiveSmallIntegerField(default=7, db_default=7),
        ),
        migrations.AddField(
            model_name="clinicconfiguration",
            name="retention_dry_run",
            field=models.BooleanField(default=True, db_default=True),
        ),
        migrations.AddField(
            model_name="clinicconfiguration",
            name="retention_backup_days",
            field=models.PositiveSmallIntegerField(default=35, db_default=35),
        ),
        migrations.AddConstraint(
            model_name="clinicconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(raw_audio_retention_days__lte=365)
                & models.Q(retention_backup_days__gte=1)
                & models.Q(retention_backup_days__lte=3650),
                name="identity_config_retention_bounds",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="serviceprincipal", name="identity_principal_v1_identity"
        ),
        migrations.AddConstraint(
            model_name="serviceprincipal",
            constraint=models.CheckConstraint(
                condition=models.Q(grant_set_version__in=(1, 2, 3))
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
                    | models.Q(grant_set_version=3, permission="retention.execute")
                )
                & models.Q(subject_scope="clinic"),
                name="identity_principal_grant_v1_scope",
            ),
        ),
        migrations.RunSQL(SQL),
    ]
