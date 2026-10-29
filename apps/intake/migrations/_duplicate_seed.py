"""Seed HMAC-only snapshots for registrations that predate duplicate detection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.intake.patient_name_index import normalized_name_bytes
from apps.tenancy.envelope import blind_indexes_with_versions

if TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def seed_duplicate_indexes(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Index existing encrypted values during owner migration, before row guards."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        # Only the trusted migration role enumerates tenant metadata. Plaintext
        # remains purpose-bound; each row is opened under its own tenant GUC.
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "SELECT DISTINCT ON (organization_id,patient_id) "
            "organization_id,patient_id,clinic_id "
            "FROM clinic_app.intake_patientclinicenrollment "
            "ORDER BY organization_id,patient_id,clinic_id"
        )
        enrollments = cursor.fetchall()
        cursor.execute("RESET ROLE")
        cursor.execute("SELECT current_setting('app.current_tenant',true)")
        previous = cursor.fetchone()
    patient_model = apps.get_model("intake", "Patient")
    contact_model = apps.get_model("intake", "PatientContact")
    index_model = apps.get_model("intake", "PatientDuplicateIndex")
    try:
        for organization_id, patient_id, clinic_id in enrollments:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_tenant',%s,true)",
                    [str(organization_id)],
                )
            patient = patient_model.objects.get(pk=patient_id)
            name_indexes = (
                blind_indexes_with_versions(
                    purpose="intake.duplicate.name_birth.v1",
                    plaintext=(
                        patient.birth_date.isoformat().encode("ascii")
                        + b":"
                        + normalized_name_bytes(patient.full_name)
                        .decode("utf-8")[:6]
                        .encode("utf-8")
                    ),
                )
                if patient.birth_date is not None
                else ()
            )
            contacts = [
                blind_indexes_with_versions(
                    purpose="intake.duplicate.contact.v1",
                    plaintext=(
                        (
                            "phone"
                            if contact.channel in ("sms", "whatsapp")
                            else contact.channel
                        )
                        + f":{contact.destination}"
                    ).encode(),
                )[-1].blind_index
                for contact in contact_model.objects.filter(patient_id=patient_id)
            ]
            version = blind_indexes_with_versions(
                purpose="intake.duplicate.contact.v1", plaintext=b"version"
            )[-1].key_version
            index_model.objects.create(
                organization_id=organization_id,
                clinic_id=clinic_id,
                patient_id=patient_id,
                version=1,
                name_birth_index=name_indexes[-1].blind_index if name_indexes else None,
                contact_indexes=contacts,
                index_key_version=version,
            )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [previous[0] or "" if previous else ""],
            )
