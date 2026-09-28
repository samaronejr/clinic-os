"""Plan item 27 addendum drafts: other clinicians write beside the main draft.

Two additive tables with FORCE RLS, least-privilege grants and the binding
guard in the same transaction; the only envelope column (addendum text) is
bytea from creation, so no protected-field conversion is involved. Reversing
drops the tables; once addenda exist, rollback is a restore.
"""

import uuid
from typing import ClassVar

import django.db.models.deletion
import django.db.models.expressions
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.tenancy.fields

from ._addendum_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Create the addendum tables, then install their database contract."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("ehr", "0010_autosave_episodes_unscheduled"),
        ("identity", "0015_encounter_permission_bundle"),
        ("intake", "0011_protected_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="EncounterAddendum",
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
                    "text",
                    apps.tenancy.fields.EncryptedTextField(
                        null=True, purpose="ehr.encounteraddendum.text"
                    ),
                ),
                (
                    "text_sha256",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "state",
                    models.CharField(
                        choices=[("draft", "Draft")], default="draft", max_length=16
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
                    "encounter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AddendumSaveReceipt",
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
                ("command_id", models.UUIDField()),
                ("request_sha256", models.CharField(max_length=64)),
                ("base_revision", models.PositiveIntegerField()),
                ("revision", models.PositiveIntegerField()),
                ("saved_at", models.DateTimeField()),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "addendum",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.encounteraddendum",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="encounteraddendum",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "draft")),
                fields=("encounter", "author"),
                name="ehr_addendum_one_draft",
            ),
        ),
        migrations.AddConstraint(
            model_name="encounteraddendum",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1), ("state__in", ["draft"])),
                name="ehr_addendum_state_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="encounteraddendum",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("text_sha256", ""),
                    ("text_sha256__regex", "^[0-9a-f]{64}$"),
                    _connector="OR",
                ),
                name="ehr_addendum_digest_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="addendumsavereceipt",
            constraint=models.UniqueConstraint(
                fields=("addendum", "command_id"),
                name="ehr_addendumreceipt_command_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="addendumsavereceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(("request_sha256__regex", "^[0-9a-f]{64}$")),
                name="ehr_addendumreceipt_digest_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="addendumsavereceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("revision", models.F("base_revision")),
                    (
                        "revision",
                        django.db.models.expressions.CombinedExpression(
                            models.F("base_revision"), "+", models.Value(1)
                        ),
                    ),
                    _connector="OR",
                ),
                name="ehr_addendumreceipt_revision_check",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
