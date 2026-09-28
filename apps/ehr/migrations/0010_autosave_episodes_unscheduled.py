"""Todo 27: unscheduled encounters, episodes and durable draft autosave.

Additive tables with FORCE RLS in the same transaction, a nullable encounter
appointment and the rewritten binding guard (H-11). No protected column is
converted: the only envelope column (episode title) is bytea from creation.
Rollback is a restore once an unscheduled encounter exists: the reverse
cannot make ``appointment_id`` NOT NULL again over such a row.
"""

import uuid
from typing import ClassVar

import django.db.models.deletion
import django.db.models.expressions
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.tenancy.fields

from ._autosave_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Create the todo 27 tables, then install their database contract."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("ehr", "0009_protected_fields"),
        ("identity", "0015_encounter_permission_bundle"),
        ("intake", "0011_protected_fields"),
        ("scheduling", "0006_appointment_lifecycle_v2"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="DraftEditState",
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
                ("section_edit_epochs", models.JSONField(default=dict)),
                ("lock_holder", models.UUIDField(blank=True, null=True)),
                ("lock_expires_at", models.DateTimeField(blank=True, null=True)),
                ("handover_requested_by", models.UUIDField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="DraftSaveReceipt",
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
            ],
        ),
        migrations.CreateModel(
            name="Episode",
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
                    "title",
                    apps.tenancy.fields.EncryptedTextField(purpose="ehr.episode.title"),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[("open", "Open"), ("closed", "Closed")],
                        default="open",
                        max_length=16,
                    ),
                ),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="EpisodeEncounter",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="encounter",
            name="unscheduled_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("walk_in", "Walk-in visit"),
                    ("phone_follow_up", "Phone follow-up"),
                    ("documentation_only", "Documentation only"),
                ],
                default="",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="encounter",
            name="appointment",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="scheduling.appointment",
            ),
        ),
        migrations.AddConstraint(
            model_name="encounter",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("appointment__isnull", False), ("unscheduled_reason", "")
                    ),
                    models.Q(
                        ("appointment__isnull", True),
                        (
                            "unscheduled_reason__in",
                            ["walk_in", "phone_follow_up", "documentation_only"],
                        ),
                    ),
                    _connector="OR",
                ),
                name="ehr_encounter_binding_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="encounter",
            constraint=models.UniqueConstraint(
                condition=models.Q(("appointment__isnull", True), ("state", "open")),
                fields=("clinic", "patient", "physician"),
                name="ehr_encounter_one_open_unscheduled",
            ),
        ),
        migrations.AddField(
            model_name="drafteditstate",
            name="author",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="drafteditstate",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="drafteditstate",
            name="version",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                to="ehr.clinicaldocumentversion",
            ),
        ),
        migrations.AddField(
            model_name="draftsavereceipt",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="draftsavereceipt",
            name="version",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="ehr.clinicaldocumentversion",
            ),
        ),
        migrations.AddField(
            model_name="episode",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="episode",
            name="closed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="episode",
            name="opened_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="episode",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="episode",
            name="patient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
            ),
        ),
        migrations.AddField(
            model_name="episodeencounter",
            name="encounter",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
            ),
        ),
        migrations.AddField(
            model_name="episodeencounter",
            name="episode",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="ehr.episode"
            ),
        ),
        migrations.AddField(
            model_name="episodeencounter",
            name="linked_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="episodeencounter",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddConstraint(
            model_name="drafteditstate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("lock_expires_at__isnull", True), ("lock_holder__isnull", True)
                    ),
                    models.Q(
                        ("lock_expires_at__isnull", False),
                        ("lock_holder__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="ehr_draftstate_lock_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="drafteditstate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("handover_requested_by__isnull", True),
                    ("lock_holder__isnull", False),
                    _connector="OR",
                ),
                name="ehr_draftstate_handover_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="draftsavereceipt",
            constraint=models.UniqueConstraint(
                fields=("version", "command_id"), name="ehr_draftreceipt_command_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="draftsavereceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(("request_sha256__regex", "^[0-9a-f]{64}$")),
                name="ehr_draftreceipt_digest_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="draftsavereceipt",
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
                name="ehr_draftreceipt_revision_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="episode",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("closed_at__isnull", True),
                        ("closed_by__isnull", True),
                        ("state", "open"),
                    ),
                    models.Q(
                        ("closed_at__isnull", False),
                        ("closed_by__isnull", False),
                        ("state", "closed"),
                    ),
                    _connector="OR",
                ),
                name="ehr_episode_state_check",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
