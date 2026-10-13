"""Create immutable representation scope and its database transition boundary."""

import uuid
from typing import ClassVar

import django.contrib.postgres.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._delegate_sql import SQL


class Migration(migrations.Migration):
    """Keep creation and privilege revocation in the same DDL transaction."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0017_workflow_bundle_subtractions"),
        ("intake", "0013_patient_demographics"),
        ("consent", "0004_purpose_taxonomy"),
        ("ehr", "0009_protected_fields"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="PatientDelegateGrant",
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
                    "authority_kind",
                    models.CharField(
                        choices=[
                            ("legal_guardian_minor", "Legal guardian of a minor"),
                            ("curator", "Curator"),
                            ("patient_authorized_adult", "Patient-authorized adult"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "purposes",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32), size=None
                    ),
                ),
                (
                    "operations",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32), size=None
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending_verification", "Pending verification"),
                            ("verified", "Verified"),
                            ("rejected", "Rejected"),
                            ("active", "Active"),
                            ("revoked", "Revoked"),
                        ],
                        default="pending_verification",
                        max_length=24,
                    ),
                ),
                ("revision", models.PositiveIntegerField(default=1)),
                ("idempotency_key", models.UUIDField()),
                ("verified_at", models.DateTimeField(null=True)),
                ("activated_at", models.DateTimeField(null=True)),
                ("revoked_at", models.DateTimeField(null=True)),
                ("expires_at", models.DateTimeField()),
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
                    "represented_enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="representation_grants",
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "delegate_enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="delegate_grants",
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "evidence_attachment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.clinicalattachment",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="delegate_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "grantor_session",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientsession",
                    ),
                ),
                (
                    "renewal_of",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.patientdelegategrant",
                    ),
                ),
                (
                    "verified_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="delegate_verifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"),
                        name="identity_delegate_org_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("clinic", "idempotency_key"),
                        name="identity_delegate_request_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            state__in=(
                                "pending_verification",
                                "verified",
                                "rejected",
                                "active",
                                "revoked",
                            )
                        )
                        & models.Q(
                            authority_kind__in=(
                                "legal_guardian_minor",
                                "curator",
                                "patient_authorized_adult",
                            )
                        )
                        & models.Q(revision__gte=1)
                        & models.Q(expires_at__gt=models.F("created_at")),
                        name="identity_delegate_lifecycle",
                    ),
                    models.CheckConstraint(
                        condition=~models.Q(
                            represented_enrollment=models.F("delegate_enrollment")
                        ),
                        name="identity_delegate_distinct_subject",
                    ),
                ],
            },
        ),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
