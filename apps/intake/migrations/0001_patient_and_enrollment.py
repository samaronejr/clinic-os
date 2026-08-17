"""Create organization patients and clinic enrollment integrity."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.intake.models
from apps.intake.rls import (
    INTAKE_RLS_TARGETS,
    apply_intake_rls,
    remove_intake_rls,
)

INTEGRITY_SQL = """
ALTER TABLE clinic_app.intake_patient
    ADD CONSTRAINT intake_patient_full_name_normalized_check
    CHECK (
        pg_catalog.char_length(full_name) BETWEEN 1 AND 255
        AND full_name = normalize(full_name, NFC)
        AND full_name = pg_catalog.regexp_replace(
            pg_catalog.btrim(full_name), '[[:space:]]+', ' ', 'g'
        )
        AND full_name !~ '[[:cntrl:]]'
    );
ALTER TABLE clinic_app.intake_patientclinicenrollment
    ADD CONSTRAINT intake_enrollment_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientclinicenrollment
    ADD CONSTRAINT intake_enrollment_org_patient_fk
    FOREIGN KEY (organization_id, patient_id)
    REFERENCES clinic_app.intake_patient (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientclinicenrollment
    ADD CONSTRAINT intake_enrollment_fingerprint_32_check
    CHECK (pg_catalog.octet_length(create_fingerprint) = 32);
"""

REVERSE_INTEGRITY_SQL = """
ALTER TABLE clinic_app.intake_patientclinicenrollment
    DROP CONSTRAINT intake_enrollment_fingerprint_32_check,
    DROP CONSTRAINT intake_enrollment_org_patient_fk,
    DROP CONSTRAINT intake_enrollment_org_clinic_fk;
ALTER TABLE clinic_app.intake_patient
    DROP CONSTRAINT IF EXISTS intake_patient_full_name_normalized_check;
"""

RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.intake_patient,
             clinic_app.intake_patientclinicenrollment
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT
    ON TABLE clinic_app.intake_patient,
             clinic_app.intake_patientclinicenrollment
    TO clinic_app;
"""

REVERSE_RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.intake_patient,
             clinic_app.intake_patientclinicenrollment
    FROM clinic_app;
"""


class Migration(migrations.Migration):
    """Install the first reversible intake schema after current foundation leaves."""

    initial = True
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0005_clinic_timezone"),
        ("tenancy", "0002_rls_and_resolvers"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="Patient",
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
                    "full_name",
                    apps.intake.models.NormalizedPatientNameField(max_length=255),
                ),
                ("birth_date", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"),
                        name="intake_patient_org_id_uniq",
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="PatientClinicEnrollment",
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
                ("idempotency_key", models.UUIDField()),
                (
                    "create_fingerprint",
                    models.BinaryField(editable=False, max_length=32),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
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
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patient",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "clinic", "patient"),
                        name="intake_enrollment_org_clinic_patient_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "clinic", "id"),
                        name="intake_enrollment_org_clinic_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "idempotency_key"),
                        name="intake_enrollment_org_idempotency_uniq",
                    ),
                ]
            },
        ),
        migrations.RunSQL(sql=INTEGRITY_SQL, reverse_sql=REVERSE_INTEGRITY_SQL),
        *(
            migrations.RunSQL(
                sql=apply_intake_rls(table, tenant_column),
                reverse_sql=remove_intake_rls(table, tenant_column),
            )
            for table, tenant_column in sorted(INTAKE_RLS_TARGETS)
        ),
        migrations.RunSQL(sql=RUNTIME_ACL_SQL, reverse_sql=REVERSE_RUNTIME_ACL_SQL),
    ]
