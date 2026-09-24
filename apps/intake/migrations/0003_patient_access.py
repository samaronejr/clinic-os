"""Create enrollment-bound patient invitations and sessions.

The runtime role can insert invitations and revoke rows, but only the
resolver-owned functions can consume a grant, mint a session, validate a
session or read the bound enrollment: ``clinic_app`` holds no INSERT on
``intake_patientsession`` and no UPDATE that could consume a grant, so a
patient session can only exist through the redemption boundary.
"""

import uuid
from typing import ClassVar, Final

import django.contrib.postgres.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.intake.rls import (
    INTAKE_RLS_TARGETS,
    apply_intake_rls,
    remove_intake_rls,
)

INTEGRITY_SQL: Final = """
ALTER TABLE clinic_app.intake_patientaccessgrant
    ADD CONSTRAINT intake_grant_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientaccessgrant
    ADD CONSTRAINT intake_grant_org_patient_fk
    FOREIGN KEY (organization_id, patient_id)
    REFERENCES clinic_app.intake_patient (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientaccessgrant
    ADD CONSTRAINT intake_grant_org_enrollment_fk
    FOREIGN KEY (organization_id, clinic_id, enrollment_id)
    REFERENCES clinic_app.intake_patientclinicenrollment
        (organization_id, clinic_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientsession
    ADD CONSTRAINT intake_session_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientsession
    ADD CONSTRAINT intake_session_org_patient_fk
    FOREIGN KEY (organization_id, patient_id)
    REFERENCES clinic_app.intake_patient (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientsession
    ADD CONSTRAINT intake_session_org_enrollment_fk
    FOREIGN KEY (organization_id, clinic_id, enrollment_id)
    REFERENCES clinic_app.intake_patientclinicenrollment
        (organization_id, clinic_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientsession
    ADD CONSTRAINT intake_session_org_grant_fk
    FOREIGN KEY (organization_id, clinic_id, grant_id)
    REFERENCES clinic_app.intake_patientaccessgrant
        (organization_id, clinic_id, id)
    NOT DEFERRABLE;
"""

REVERSE_INTEGRITY_SQL: Final = """
ALTER TABLE clinic_app.intake_patientsession
    DROP CONSTRAINT intake_session_org_grant_fk,
    DROP CONSTRAINT intake_session_org_enrollment_fk,
    DROP CONSTRAINT intake_session_org_patient_fk,
    DROP CONSTRAINT intake_session_org_clinic_fk;
ALTER TABLE clinic_app.intake_patientaccessgrant
    DROP CONSTRAINT intake_grant_org_enrollment_fk,
    DROP CONSTRAINT intake_grant_org_patient_fk,
    DROP CONSTRAINT intake_grant_org_clinic_fk;
"""

FUNCTIONS_SQL: Final = """
SET LOCAL ROLE clinic_resolver;

CREATE FUNCTION clinic_app.redeem_patient_invitation(
    requested_clinic pg_catalog.uuid,
    requested_hash pg_catalog.bytea
)
RETURNS TABLE(session_id pg_catalog.uuid)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
BEGIN
    RETURN QUERY
    WITH consumed AS (
        UPDATE clinic_app.intake_patientaccessgrant AS invitation
        SET consumed_at = pg_catalog.now()
        WHERE invitation.secret_hash = requested_hash
          AND invitation.clinic_id = requested_clinic
          AND invitation.consumed_at IS NULL
          AND invitation.revoked_at IS NULL
          AND invitation.expires_at > pg_catalog.now()
        RETURNING invitation.id,
                  invitation.organization_id,
                  invitation.clinic_id,
                  invitation.patient_id,
                  invitation.enrollment_id,
                  invitation.operations
    )
    INSERT INTO clinic_app.intake_patientsession AS session_row (
        id, organization_id, clinic_id, patient_id, enrollment_id,
        grant_id, operations, expires_at, idle_expires_at, created_at
    )
    SELECT pg_catalog.gen_random_uuid(),
           consumed.organization_id,
           consumed.clinic_id,
           consumed.patient_id,
           consumed.enrollment_id,
           consumed.id,
           consumed.operations,
           pg_catalog.now() + pg_catalog.interval '8 hours',
           pg_catalog.now() + pg_catalog.interval '30 minutes',
           pg_catalog.now()
    FROM consumed
    RETURNING session_row.id;
END;
$function$;

CREATE FUNCTION clinic_app.touch_patient_session(
    requested_session pg_catalog.uuid
)
RETURNS TABLE(
    session_id pg_catalog.uuid,
    organization_id pg_catalog.uuid,
    clinic_id pg_catalog.uuid,
    patient_id pg_catalog.uuid,
    enrollment_id pg_catalog.uuid,
    operations pg_catalog.text[],
    expires_at pg_catalog.timestamptz,
    idle_expires_at pg_catalog.timestamptz
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
BEGIN
    RETURN QUERY
    UPDATE clinic_app.intake_patientsession AS session_row
    SET idle_expires_at = pg_catalog.now() + pg_catalog.interval '30 minutes'
    WHERE session_row.id = requested_session
      AND session_row.revoked_at IS NULL
      AND session_row.expires_at > pg_catalog.now()
      AND session_row.idle_expires_at > pg_catalog.now()
    RETURNING session_row.id,
              session_row.organization_id,
              session_row.clinic_id,
              session_row.patient_id,
              session_row.enrollment_id,
              session_row.operations::pg_catalog.text[],
              session_row.expires_at,
              session_row.idle_expires_at;
END;
$function$;

CREATE FUNCTION clinic_app.end_patient_session(
    requested_session pg_catalog.uuid
)
RETURNS pg_catalog.void
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    UPDATE clinic_app.intake_patientsession AS session_row
    SET revoked_at = pg_catalog.now()
    WHERE session_row.id = requested_session
      AND session_row.revoked_at IS NULL
$function$;

CREATE FUNCTION clinic_app.patient_session_overview()
RETURNS TABLE(
    session_id pg_catalog.uuid,
    patient_name pg_catalog.varchar,
    clinic_name pg_catalog.varchar,
    enrolled_at pg_catalog.timestamptz,
    operations pg_catalog.text[],
    expires_at pg_catalog.timestamptz,
    idle_expires_at pg_catalog.timestamptz
)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
ROWS 1
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT session_row.id,
           patient.full_name,
           clinic.name,
           enrollment.created_at,
           session_row.operations::pg_catalog.text[],
           session_row.expires_at,
           session_row.idle_expires_at
    FROM clinic_app.intake_patientsession AS session_row
    JOIN clinic_app.intake_patient AS patient
      ON patient.organization_id = session_row.organization_id
     AND patient.id = session_row.patient_id
    JOIN clinic_app.identity_clinic AS clinic
      ON clinic.organization_id = session_row.organization_id
     AND clinic.id = session_row.clinic_id
    JOIN clinic_app.intake_patientclinicenrollment AS enrollment
      ON enrollment.organization_id = session_row.organization_id
     AND enrollment.clinic_id = session_row.clinic_id
     AND enrollment.id = session_row.enrollment_id
    WHERE session_row.id = NULLIF(
        pg_catalog.current_setting('app.current_patient_session', true),
        ''
    )::pg_catalog.uuid
      AND session_row.revoked_at IS NULL
      AND session_row.expires_at > pg_catalog.now()
      AND session_row.idle_expires_at > pg_catalog.now()
$function$;

REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.redeem_patient_invitation(
        pg_catalog.uuid, pg_catalog.bytea)
    FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.touch_patient_session(pg_catalog.uuid)
    FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.end_patient_session(pg_catalog.uuid)
    FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.patient_session_overview()
    FROM PUBLIC;

GRANT EXECUTE
    ON FUNCTION clinic_app.redeem_patient_invitation(
        pg_catalog.uuid, pg_catalog.bytea)
    TO clinic_app;
GRANT EXECUTE
    ON FUNCTION clinic_app.touch_patient_session(pg_catalog.uuid)
    TO clinic_app;
GRANT EXECUTE
    ON FUNCTION clinic_app.end_patient_session(pg_catalog.uuid)
    TO clinic_app;
GRANT EXECUTE
    ON FUNCTION clinic_app.patient_session_overview()
    TO clinic_app;

RESET ROLE;
"""

REVERSE_FUNCTIONS_SQL: Final = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.redeem_patient_invitation(
    pg_catalog.uuid, pg_catalog.bytea);
DROP FUNCTION IF EXISTS clinic_app.touch_patient_session(pg_catalog.uuid);
DROP FUNCTION IF EXISTS clinic_app.end_patient_session(pg_catalog.uuid);
DROP FUNCTION IF EXISTS clinic_app.patient_session_overview();
RESET ROLE;
"""

RUNTIME_ACL_SQL: Final = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.intake_patientaccessgrant,
             clinic_app.intake_patientsession
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT
    ON TABLE clinic_app.intake_patientaccessgrant
    TO clinic_app;
GRANT UPDATE (revoked_at)
    ON TABLE clinic_app.intake_patientaccessgrant
    TO clinic_app;
GRANT SELECT
    ON TABLE clinic_app.intake_patientsession
    TO clinic_app;
GRANT UPDATE (revoked_at)
    ON TABLE clinic_app.intake_patientsession
    TO clinic_app;
GRANT SELECT, UPDATE (consumed_at)
    ON TABLE clinic_app.intake_patientaccessgrant
    TO clinic_resolver;
GRANT SELECT, INSERT, UPDATE (idle_expires_at, revoked_at)
    ON TABLE clinic_app.intake_patientsession
    TO clinic_resolver;
GRANT SELECT
    ON TABLE clinic_app.intake_patient,
             clinic_app.intake_patientclinicenrollment
    TO clinic_resolver;
"""

REVERSE_RUNTIME_ACL_SQL: Final = """
REVOKE SELECT
    ON TABLE clinic_app.intake_patient,
             clinic_app.intake_patientclinicenrollment
    FROM clinic_resolver;
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.intake_patientaccessgrant,
             clinic_app.intake_patientsession
    FROM clinic_app, clinic_resolver;
"""


class Migration(migrations.Migration):
    """Install the invitation/session schema and its resolver functions."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("intake", "0002_contacts_and_preferences"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="PatientAccessGrant",
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
                ("issued_by_label", models.CharField(max_length=150)),
                ("secret_hash", models.BinaryField(editable=False, max_length=32)),
                (
                    "operations",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32),
                        size=None,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "issued_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
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
                        fields=("organization", "id"),
                        name="intake_grant_org_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "clinic", "id"),
                        name="intake_grant_org_clinic_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("secret_hash",),
                        name="intake_grant_secret_hash_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.expressions.RawSQL(
                            "operations::pg_catalog.text[] <@ %s::text[] AND "
                            "pg_catalog.array_length(operations, 1) > 0",
                            (["enrollment_view"],),
                            output_field=models.BooleanField(),
                        ),
                        name="intake_grant_operations_check",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(expires_at__gt=models.F("created_at")),
                        name="intake_grant_expiry_check",
                    ),
                    models.CheckConstraint(
                        condition=models.expressions.RawSQL(
                            "pg_catalog.octet_length(secret_hash) = 32",
                            (),
                            output_field=models.BooleanField(),
                        ),
                        name="intake_grant_secret_hash_32_check",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="PatientSession",
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
                    "operations",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32),
                        size=None,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("idle_expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "grant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientaccessgrant",
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
                        fields=("organization", "id"),
                        name="intake_session_org_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "clinic", "id"),
                        name="intake_session_org_clinic_id_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.expressions.RawSQL(
                            "operations::pg_catalog.text[] <@ %s::text[] AND "
                            "pg_catalog.array_length(operations, 1) > 0",
                            (["enrollment_view"],),
                            output_field=models.BooleanField(),
                        ),
                        name="intake_session_operations_check",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(expires_at__gt=models.F("created_at")),
                        name="intake_session_expiry_check",
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
            if table
            in {
                "intake_patientaccessgrant",
                "intake_patientsession",
            }
        ),
        migrations.RunSQL(sql=FUNCTIONS_SQL, reverse_sql=REVERSE_FUNCTIONS_SQL),
        migrations.RunSQL(sql=RUNTIME_ACL_SQL, reverse_sql=REVERSE_RUNTIME_ACL_SQL),
    ]
