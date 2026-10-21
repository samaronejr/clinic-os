"""Versioned bundles and enveloped records; irreversible, rollback = restore."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields
from apps.ehr.migrations._longitudinal_operations import ProtectedSchema
from apps.ehr.migrations._pack_sql import SQL

if TYPE_CHECKING:
    from django.db.migrations.operations.base import Operation

PROTECT = django.db.models.deletion.PROTECT


MODELS: list[Operation] = [
    migrations.CreateModel(
        name="SpecialtyPack",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    primary_key=True,
                    serialize=False,
                    editable=False,
                ),
            ),
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            ("key", models.SlugField(max_length=32)),
            ("version", models.PositiveIntegerField()),
            ("position", models.PositiveIntegerField(default=1)),
            (
                "template",
                models.OneToOneField(on_delete=PROTECT, to="ehr.specialtytemplate"),
            ),
            (
                "questionnaire",
                models.OneToOneField(
                    on_delete=PROTECT, to="intake.questionnairetemplate"
                ),
            ),
            ("calculators", models.JSONField(default=list)),
            ("license_ref", models.CharField(max_length=160)),
            (
                "clinical_owner",
                models.ForeignKey(
                    on_delete=PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                ),
            ),
            ("installed_at", models.DateTimeField(auto_now_add=True)),
            ("uninstalled_at", models.DateTimeField(null=True)),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "key", "version"), name="ehr_pack_version_unique"
                ),
                models.CheckConstraint(
                    condition=models.Q(
                        key__in=["general", "pediatrics", "psychiatry", "procedures"],
                        version__gte=1,
                    ),
                    name="ehr_pack_catalog_check",
                ),
                models.CheckConstraint(
                    condition=models.Q(position__gte=1, position__lte=4),
                    name="ehr_pack_position_check",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="SpecialtyEntry",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    primary_key=True,
                    serialize=False,
                    editable=False,
                ),
            ),
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            ("pack", models.ForeignKey(on_delete=PROTECT, to="ehr.specialtypack")),
            ("encounter", models.ForeignKey(on_delete=PROTECT, to="ehr.encounter")),
            ("kind", models.CharField(max_length=16)),
            (
                "details",
                apps.tenancy.fields.EncryptedJSONField(
                    purpose="ehr.specialtyentry.details"
                ),
            ),
            (
                "consent_document",
                models.ForeignKey(
                    on_delete=PROTECT,
                    related_name="+",
                    to="ehr.clinicalattachment",
                    null=True,
                ),
            ),
            ("images", models.JSONField(default=list)),
            (
                "recorded_by",
                models.ForeignKey(
                    on_delete=PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
        ],
        options={
            "constraints": [
                models.CheckConstraint(
                    condition=models.Q(kind__in=["immunization", "procedure"]),
                    name="ehr_specialty_entry_kind",
                ),
            ]
        },
    ),
]


class Migration(migrations.Migration):
    """Publish the protected table and its least-privilege contract atomically."""

    atomic = False
    dependencies: ClassVar = [
        ("ehr", "0014_longitudinal_record"),
        ("intake", "0015_questionnaire_branching"),
    ]
    operations: ClassVar = [
        ProtectedSchema(
            database_operations=[*MODELS, migrations.RunSQL(SQL)],
            state_operations=MODELS,
        )
    ]
