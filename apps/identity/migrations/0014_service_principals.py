"""Add login-bound machine authority without any staff impersonation grants."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models

from ._principal_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Create authority tables and revoke default DML in the same transaction."""

    dependencies: ClassVar = [
        ("identity", "0013_permission_bundles"),
        ("scheduling", "0004_waitlistentry_waitlistoffer_and_more"),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="ServicePrincipal",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.SlugField(max_length=64)),
                ("db_identity", models.CharField(max_length=63, unique=True)),
                ("purpose", models.SlugField(max_length=64)),
                ("grant_set_version", models.PositiveSmallIntegerField(default=1)),
                ("active", models.BooleanField(default=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ServicePrincipalGrant",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("permission", models.CharField(max_length=64)),
                ("subject_scope", models.CharField(default="clinic", max_length=16)),
                ("active", models.BooleanField(default=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "principal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.serviceprincipal",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="serviceprincipal",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="identity_principal_org_id_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="serviceprincipal",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("grant_set_version", 1),
                    ("db_identity__regex", "^clinic_agent(_[a-z0-9_]+)?$"),
                    ("name__regex", "^[a-z0-9][a-z0-9_-]{0,63}$"),
                    ("purpose__regex", "^[a-z0-9][a-z0-9_-]{0,63}$"),
                ),
                name="identity_principal_v1_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="serviceprincipalgrant",
            constraint=models.UniqueConstraint(
                condition=models.Q(("active", True)),
                fields=("principal", "permission", "subject_scope"),
                name="identity_principal_active_grant",
            ),
        ),
        migrations.AddConstraint(
            model_name="serviceprincipalgrant",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("permission", "appointment.read"), ("subject_scope", "clinic")
                ),
                name="identity_principal_grant_v1_scope",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
