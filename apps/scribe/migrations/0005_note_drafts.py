"""Add protected note drafts and retained, clinician-attributed provenance."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields
from apps.scribe.migrations._draft_sql import SQL


class Migration(migrations.Migration):
    """Protected append-only drafts; irreversible, rollback requires restore."""

    atomic = False

    dependencies: ClassVar = [
        ("ai", "0002_agent_execution"),
        ("consent", "0005_delegate_purposes"),
        ("ehr", "0016_later_pack_draft_guard"),
        ("identity", "0040_agent_execution_authority"),
        ("scribe", "0004_purge_state"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="AIDraft",
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
                ("request_key", models.UUIDField()),
                ("generation", models.PositiveIntegerField()),
                ("base_revision", models.PositiveIntegerField()),
                ("base_epochs", models.JSONField()),
                (
                    "base_content",
                    apps.tenancy.fields.EncryptedJSONField(purpose="scribe.draft.base"),
                ),
                ("sections", models.JSONField()),
                ("source_refs", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "consent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="consent.consentacceptance",
                    ),
                ),
                (
                    "encounter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
                    ),
                ),
                (
                    "invocation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ai.aiinvocation",
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
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "transcript",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scribe.transcriptversion",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.clinicaldocumentversion",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="DraftActionProposal",
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
                ("ordinal", models.PositiveSmallIntegerField()),
                ("kind", models.CharField(max_length=8)),
                (
                    "text",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="scribe.draft.proposal"
                    ),
                ),
                ("citations", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "draft",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="scribe.aidraft"
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
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.clinicaldocumentversion",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="DraftSectionSuggestion",
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
                ("section", models.CharField(max_length=16)),
                (
                    "text",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="scribe.draft.suggestion"
                    ),
                ),
                ("citations", models.JSONField()),
                ("assertion_class", models.CharField(max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "draft",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="scribe.aidraft"
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
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.clinicaldocumentversion",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="ReviewDecision",
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
                ("request_digest", models.CharField(max_length=64)),
                ("action", models.CharField(max_length=8)),
                ("section", models.CharField(max_length=16)),
                ("expected_revision", models.PositiveIntegerField()),
                ("result_revision", models.PositiveIntegerField()),
                ("result_sha256", models.CharField(max_length=64)),
                (
                    "applied_text",
                    apps.tenancy.fields.EncryptedJSONField(
                        purpose="scribe.review.applied", null=True
                    ),
                ),
                ("capability_snapshot", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "suggestion",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scribe.draftsectionsuggestion",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.clinicaldocumentversion",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="aidraft",
            constraint=models.UniqueConstraint(
                fields=("version", "generation"), name="scribe_draft_generation"
            ),
        ),
        migrations.AddConstraint(
            model_name="aidraft",
            constraint=models.UniqueConstraint(
                fields=("version", "request_key"), name="scribe_draft_request_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="draftactionproposal",
            constraint=models.UniqueConstraint(
                fields=("draft", "ordinal"), name="scribe_draft_proposal"
            ),
        ),
        migrations.AddConstraint(
            model_name="draftsectionsuggestion",
            constraint=models.UniqueConstraint(
                fields=("draft", "section"), name="scribe_draft_section"
            ),
        ),
        migrations.AddConstraint(
            model_name="reviewdecision",
            constraint=models.UniqueConstraint(
                fields=("version", "command_id"), name="scribe_review_command"
            ),
        ),
        migrations.RunSQL(SQL),
    ]
