"""Create verified contacts, channel preferences and append-only history."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.intake.rls import (
    INTAKE_RLS_TARGETS,
    apply_intake_rls,
    remove_intake_rls,
)

INTEGRITY_SQL = """
ALTER TABLE clinic_app.intake_patientcontact
    ADD CONSTRAINT intake_contact_org_patient_fk
    FOREIGN KEY (organization_id, patient_id)
    REFERENCES clinic_app.intake_patient (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientchannelpreference
    ADD CONSTRAINT intake_preference_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientchannelpreference
    ADD CONSTRAINT intake_preference_org_patient_fk
    FOREIGN KEY (organization_id, patient_id)
    REFERENCES clinic_app.intake_patient (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientcontactevent
    ADD CONSTRAINT intake_contact_event_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientcontactevent
    ADD CONSTRAINT intake_contact_event_org_patient_fk
    FOREIGN KEY (organization_id, patient_id)
    REFERENCES clinic_app.intake_patient (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientcontactevent
    ADD CONSTRAINT intake_contact_event_org_contact_fk
    FOREIGN KEY (organization_id, contact_id)
    REFERENCES clinic_app.intake_patientcontact (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.intake_patientcontactevent
    ADD CONSTRAINT intake_contact_event_org_preference_fk
    FOREIGN KEY (organization_id, clinic_id, preference_id)
    REFERENCES clinic_app.intake_patientchannelpreference
        (organization_id, clinic_id, id)
    NOT DEFERRABLE;

CREATE FUNCTION clinic_app.intake_contact_event_immutable_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        CONSTRAINT = 'intake_contact_event_immutable_check',
        MESSAGE = 'patient contact history is append-only';
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.intake_contact_event_immutable_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER intake_contact_event_immutable
BEFORE UPDATE OR DELETE ON clinic_app.intake_patientcontactevent
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_contact_event_immutable_v1();
"""

REVERSE_INTEGRITY_SQL = """
DROP TRIGGER IF EXISTS intake_contact_event_immutable
    ON clinic_app.intake_patientcontactevent;
DROP FUNCTION IF EXISTS clinic_app.intake_contact_event_immutable_v1();
ALTER TABLE clinic_app.intake_patientcontactevent
    DROP CONSTRAINT intake_contact_event_org_preference_fk,
    DROP CONSTRAINT intake_contact_event_org_contact_fk,
    DROP CONSTRAINT intake_contact_event_org_patient_fk,
    DROP CONSTRAINT intake_contact_event_org_clinic_fk;
ALTER TABLE clinic_app.intake_patientchannelpreference
    DROP CONSTRAINT intake_preference_org_patient_fk,
    DROP CONSTRAINT intake_preference_org_clinic_fk;
ALTER TABLE clinic_app.intake_patientcontact
    DROP CONSTRAINT intake_contact_org_patient_fk;
"""

RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.intake_patientcontact,
             clinic_app.intake_patientchannelpreference,
             clinic_app.intake_patientcontactevent
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT
    ON TABLE clinic_app.intake_patientcontact,
             clinic_app.intake_patientchannelpreference,
             clinic_app.intake_patientcontactevent
    TO clinic_app;
GRANT UPDATE (
    destination, destination_version, verified_version, verified_at,
    verification_method, updated_at
)
    ON TABLE clinic_app.intake_patientcontact
    TO clinic_app;
GRANT UPDATE (opted_in, version, updated_at)
    ON TABLE clinic_app.intake_patientchannelpreference
    TO clinic_app;
"""

REVERSE_RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.intake_patientcontact,
             clinic_app.intake_patientchannelpreference,
             clinic_app.intake_patientcontactevent
    FROM clinic_app;
"""


class Migration(migrations.Migration):
    """Install the contact/preference schema after the enrollment leaves."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("intake", "0001_patient_and_enrollment"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="PatientContact",
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
                    "channel",
                    models.CharField(
                        choices=[
                            ("sms", "SMS"),
                            ("email", "Email"),
                            ("whatsapp", "WhatsApp"),
                        ],
                        max_length=32,
                    ),
                ),
                ("destination", models.CharField(max_length=255)),
                ("destination_version", models.PositiveIntegerField(default=1)),
                ("verified_version", models.PositiveIntegerField(default=0)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("verification_method", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
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
                        fields=("organization", "patient", "channel"),
                        name="intake_contact_org_patient_channel_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "id"),
                        name="intake_contact_org_id_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(channel__in=["sms", "email", "whatsapp"]),
                        name="intake_contact_channel_check",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            verified_version__lte=models.F("destination_version")
                        ),
                        name="intake_contact_version_check",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(verified_version__gt=0)
                            & models.Q(verified_at__isnull=False)
                            & ~models.Q(verification_method="")
                        )
                        | (
                            models.Q(verified_version=0)
                            & models.Q(verified_at__isnull=True)
                            & models.Q(verification_method="")
                        ),
                        name="intake_contact_verification_check",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="PatientChannelPreference",
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
                    "purpose",
                    models.CharField(
                        choices=[
                            ("appointment_reminder", "Appointment reminder"),
                            ("booking_confirmation", "Booking confirmation"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("sms", "SMS"),
                            ("email", "Email"),
                            ("whatsapp", "WhatsApp"),
                        ],
                        max_length=32,
                    ),
                ),
                ("opted_in", models.BooleanField(default=False)),
                ("version", models.PositiveIntegerField(default=1)),
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
                        fields=(
                            "organization",
                            "clinic",
                            "patient",
                            "purpose",
                            "channel",
                        ),
                        name="intake_preference_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "clinic", "id"),
                        name="intake_preference_org_clinic_id_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            purpose__in=[
                                "appointment_reminder",
                                "booking_confirmation",
                            ]
                        ),
                        name="intake_preference_purpose_check",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(channel__in=["sms", "email", "whatsapp"]),
                        name="intake_preference_channel_check",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="PatientContactEvent",
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
                    "event_type",
                    models.CharField(
                        choices=[
                            ("contact_saved", "Contact saved"),
                            ("contact_verified", "Contact verified"),
                            (
                                "verification_invalidated",
                                "Verification invalidated",
                            ),
                            ("preference_opted_in", "Preference opted in"),
                            ("preference_opted_out", "Preference opted out"),
                        ],
                        max_length=32,
                    ),
                ),
                ("version", models.PositiveIntegerField()),
                ("actor_label", models.CharField(max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
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
                    "contact",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientcontact",
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
                (
                    "preference",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientchannelpreference",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"),
                        name="intake_contact_event_org_id_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            event_type__in=[
                                "contact_saved",
                                "contact_verified",
                                "verification_invalidated",
                                "preference_opted_in",
                                "preference_opted_out",
                            ]
                        ),
                        name="intake_contact_event_type_check",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(contact__isnull=False)
                            & models.Q(preference__isnull=True)
                            & models.Q(
                                event_type__in=[
                                    "contact_saved",
                                    "contact_verified",
                                    "verification_invalidated",
                                ]
                            )
                        )
                        | (
                            models.Q(contact__isnull=True)
                            & models.Q(preference__isnull=False)
                            & models.Q(
                                event_type__in=[
                                    "preference_opted_in",
                                    "preference_opted_out",
                                ]
                            )
                        ),
                        name="intake_contact_event_subject_check",
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
                "intake_patientcontact",
                "intake_patientchannelpreference",
                "intake_patientcontactevent",
            }
        ),
        migrations.RunSQL(sql=RUNTIME_ACL_SQL, reverse_sql=REVERSE_RUNTIME_ACL_SQL),
    ]
