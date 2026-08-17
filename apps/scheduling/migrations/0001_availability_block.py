"""Create immutable practitioner availability with active overlap protection."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    DateTimeRangeField,
    RangeBoundary,
    RangeOperators,
)
from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.scheduling.rls import (
    SCHEDULING_RLS_TARGETS,
    apply_scheduling_rls,
    remove_scheduling_rls,
)

INTEGRITY_SQL = """
ALTER TABLE clinic_app.scheduling_availabilityblock
    ADD CONSTRAINT scheduling_availability_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.scheduling_availabilityblock
    ADD CONSTRAINT scheduling_availability_positive_minute_range_check
    CHECK (
        end_at > start_at
        AND pg_catalog.date_trunc('minute', start_at) = start_at
        AND pg_catalog.date_trunc('minute', end_at) = end_at
    );
ALTER TABLE clinic_app.scheduling_availabilityblock
    ADD CONSTRAINT scheduling_availability_fingerprint_32_check
    CHECK (pg_catalog.octet_length(create_fingerprint) = 32);
ALTER TABLE clinic_app.scheduling_availabilityblock
    ADD CONSTRAINT scheduling_availability_retirement_check
    CHECK (retired_at IS NULL OR retired_at >= created_at);
"""

REVERSE_INTEGRITY_SQL = """
ALTER TABLE clinic_app.scheduling_availabilityblock
    DROP CONSTRAINT scheduling_availability_retirement_check,
    DROP CONSTRAINT scheduling_availability_fingerprint_32_check,
    DROP CONSTRAINT scheduling_availability_positive_minute_range_check,
    DROP CONSTRAINT scheduling_availability_org_clinic_fk;
"""

RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.scheduling_availabilityblock
    FROM PUBLIC, clinic_app, clinic_resolver;
REVOKE ALL PRIVILEGES (
    id, organization_id, clinic_id, practitioner_id, start_at, end_at,
    idempotency_key, create_fingerprint, retired_at, created_at, updated_at
)
    ON TABLE clinic_app.scheduling_availabilityblock
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT
    ON TABLE clinic_app.scheduling_availabilityblock
    TO clinic_app;
GRANT UPDATE (retired_at, updated_at)
    ON TABLE clinic_app.scheduling_availabilityblock
    TO clinic_app;
"""

REVERSE_RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.scheduling_availabilityblock
    FROM clinic_app;
REVOKE ALL PRIVILEGES (retired_at, updated_at)
    ON TABLE clinic_app.scheduling_availabilityblock
    FROM clinic_app;
"""

INDEX_SQL = """
CREATE INDEX scheduling_availability_clinic_start_idx
    ON clinic_app.scheduling_availabilityblock (clinic_id, start_at);
CREATE INDEX scheduling_availability_practitioner_start_idx
    ON clinic_app.scheduling_availabilityblock (practitioner_id, start_at);
"""

REVERSE_INDEX_SQL = """
DROP INDEX IF EXISTS clinic_app.scheduling_availability_practitioner_start_idx;
DROP INDEX IF EXISTS clinic_app.scheduling_availability_clinic_start_idx;
"""


class Migration(migrations.Migration):
    """Install availability after both current identity and intake leaves."""

    initial = True
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0005_clinic_timezone"),
        ("intake", "0001_patient_and_enrollment"),
    ]
    operations: ClassVar[list[Operation]] = [
        BtreeGistExtension(),
        migrations.CreateModel(
            name="AvailabilityBlock",
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
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                ("idempotency_key", models.UUIDField()),
                (
                    "create_fingerprint",
                    models.BinaryField(editable=False, max_length=32),
                ),
                ("retired_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
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
                (
                    "practitioner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "idempotency_key"),
                        name="scheduling_availability_org_idempotency_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "clinic", "id"),
                        name="scheduling_availability_org_clinic_id_uniq",
                    ),
                    ExclusionConstraint(
                        name="scheduling_availability_active_practitioner_excl",
                        expressions=(
                            ("practitioner", RangeOperators.EQUAL),
                            (
                                models.Func(
                                    "start_at",
                                    "end_at",
                                    RangeBoundary(),
                                    function="TSTZRANGE",
                                    output_field=DateTimeRangeField(),
                                ),
                                RangeOperators.OVERLAPS,
                            ),
                        ),
                        condition=models.Q(retired_at__isnull=True),
                    ),
                ],
            },
        ),
        migrations.RunSQL(sql=INTEGRITY_SQL, reverse_sql=REVERSE_INTEGRITY_SQL),
        *(
            migrations.RunSQL(
                sql=apply_scheduling_rls(table, tenant_column),
                reverse_sql=remove_scheduling_rls(table, tenant_column),
            )
            for table, tenant_column in sorted(SCHEDULING_RLS_TARGETS)
        ),
        migrations.RunSQL(sql=RUNTIME_ACL_SQL, reverse_sql=REVERSE_RUNTIME_ACL_SQL),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=INDEX_SQL, reverse_sql=REVERSE_INDEX_SQL),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="availabilityblock",
                    index=models.Index(
                        fields=("clinic", "start_at"),
                        name="sched_avail_clinic_start_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="availabilityblock",
                    index=models.Index(
                        fields=("practitioner", "start_at"),
                        name="sched_avail_pract_start_idx",
                    ),
                ),
            ],
        ),
    ]
