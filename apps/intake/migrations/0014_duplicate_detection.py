"""Add HMAC-only duplicate snapshots and retained human-review candidates."""

import uuid
from typing import ClassVar

import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._duplicate_detection_sql import FINALIZE_SQL, SQL, table_sql
from apps.intake.migrations._duplicate_seed import seed_duplicate_indexes
from apps.intake.rls import apply_intake_rls
from apps.tenancy.migrations_support import revoke_default_dml

TABLES = ("intake_duplicatecandidate", "intake_patientduplicateindex")


class Migration(migrations.Migration):
    """Retain detection history; rollback requires an owned database restore."""

    dependencies: ClassVar = [("intake", "0014_delegate_sessions")]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="PatientDuplicateIndex",
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
                ("version", models.PositiveIntegerField()),
                (
                    "name_birth_index",
                    models.BinaryField(editable=False, max_length=32, null=True),
                ),
                (
                    "contact_indexes",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.BinaryField(max_length=32),
                        default=list,
                        editable=False,
                        size=None,
                    ),
                ),
                ("index_key_version", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "patient", "version"),
                        name="intake_duplicate_index_version_uniq",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="DuplicateCandidate",
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
                (
                    "signals",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32), size=None
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("dismissed", "Dismissed"),
                            ("merged", "Merged"),
                        ],
                        default="open",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "enrollment_a",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="duplicate_candidates_a",
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "enrollment_b",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="duplicate_candidates_b",
                        to="intake.patientclinicenrollment",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "organization",
                            "clinic",
                            "enrollment_a",
                            "enrollment_b",
                        ),
                        name="intake_duplicate_candidate_pair_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(enrollment_a__lt=models.F("enrollment_b")),
                        name="intake_duplicate_candidate_order_check",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(state__in=("open", "dismissed", "merged")),
                        name="intake_duplicate_candidate_state_check",
                    ),
                ],
            },
        ),
        migrations.RunPython(seed_duplicate_indexes),
        migrations.RunSQL(
            "".join(
                revoke_default_dml(table, ("SELECT", "INSERT"))
                + apply_intake_rls(table, "organization_id")
                for table in TABLES
            )
            + SQL
            + "".join(table_sql(table) for table in TABLES)
            + FINALIZE_SQL
        ),
    ]
