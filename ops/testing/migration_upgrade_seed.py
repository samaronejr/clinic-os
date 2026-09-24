"""Seed representative pre-upgrade data through the *base* revision.

This file is piped into ``manage.py shell`` inside a worktree checked out
at the verified PR base, so it may only use schema and service entrypoints
that exist there. It writes one organization with a clinic, an owner user
and clinic role, one patient + enrollment, one availability block, one
appointment, and two hash-chained audit events — the representative
patient/scheduling/role/audit state the candidate upgrade must preserve.

Marker values are exported to ``$UPGRADE_SEED_FILE`` as JSON so the
post-migration verifier can bind to exactly these rows.
"""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from apps.audit.canonical import AuditEventInput
from apps.audit.services import record_event
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.intake.patient_creation import create_patient
from apps.scheduling.appointment_creation import create_appointment
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.availability_creation import create_availability
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import issue_tenant_key
from django.db import connection, transaction

ORGANIZATION_ID = uuid4()
CLINIC_ID = uuid4()
OWNER_ID = uuid4()
PRACTITIONER_ID = uuid4()
OWNER_USERNAME = f"upgrade-owner-{uuid4().hex[:8]}"
PRACTITIONER_USERNAME = f"upgrade-md-{uuid4().hex[:8]}"
PATIENT_NAME = "Synthetic Upgrade Persona"
PATIENT_BIRTH_DATE = date(1988, 4, 17)

with transaction.atomic(), connection.cursor() as cursor:
    cursor.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        [str(ORGANIZATION_ID)],
    )
    organization = Organization.objects.create(
        id=ORGANIZATION_ID,
        name="Synthetic Upgrade Organization",
        cnpj=f"{int(ORGANIZATION_ID) % 10**14:014d}",
    )
    clinic = Clinic.objects.create(
        id=CLINIC_ID,
        organization=organization,
        name="Synthetic Upgrade Clinic",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    owner = User.objects.create(
        id=OWNER_ID,
        username=OWNER_USERNAME,
    )
    practitioner = User.objects.create(
        id=PRACTITIONER_ID,
        username=PRACTITIONER_USERNAME,
    )
    UserClinicRole.objects.create(
        user=owner,
        organization=organization,
        clinic=clinic,
        role=UserClinicRole.Role.OWNER,
    )
    UserClinicRole.objects.create(
        user=practitioner,
        organization=organization,
        clinic=clinic,
        role=UserClinicRole.Role.PHYSICIAN,
    )
    # The tenant DEK must exist before clinic_app writes reach any
    # envelope-protected field.
    issue_tenant_key()

with connection.cursor() as cursor:
    cursor.execute("SET ROLE clinic_app")
try:
    with tenant_context(OWNER_ID, ORGANIZATION_ID):
        registration = create_patient(
            clinic_id=CLINIC_ID,
            full_name=PATIENT_NAME,
            birth_date=PATIENT_BIRTH_DATE,
            idempotency_key=uuid4(),
        )
        create_availability(
            clinic_id=CLINIC_ID,
            practitioner_id=PRACTITIONER_ID,
            start_local="2035-08-03T08:00",
            end_local="2035-08-03T12:00",
            idempotency_key=uuid4(),
        )
        appointment = create_appointment(
            clinic_id=CLINIC_ID,
            enrollment_id=registration.enrollment.pk,
            practitioner_id=PRACTITIONER_ID,
            local_range=AppointmentLocalRange("2035-08-03T09:00", "2035-08-03T10:00"),
            idempotency_key=uuid4(),
        )
        for index in range(2):
            record_event(
                AuditEventInput(
                    event_type="upgrade.seed.marker",
                    component_id="migration-upgrade-gate",
                    component_ip=None,
                    affected_record_type="upgrade.seed",
                    affected_record_id=str(appointment.pk),
                    occurred_at_utc=datetime.now(UTC),
                ),
                payload={
                    "clinic_id": str(CLINIC_ID),
                    "object_verb": "upgrade.seed",
                    "request_id": str(uuid4()),
                    "reason_code": f"seed-marker-{index}",
                },
            )
finally:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")

markers = {
    "appointment_id": str(appointment.pk),
    "audit_events": 2,
    "clinic_id": str(CLINIC_ID),
    "organization_id": str(ORGANIZATION_ID),
    "owner_id": str(OWNER_ID),
    "patient_birth_date": PATIENT_BIRTH_DATE.isoformat(),
    "patient_id": str(registration.patient.pk),
    "practitioner_id": str(PRACTITIONER_ID),
    "patient_name": PATIENT_NAME,
}
seed_path = os.environ.get("UPGRADE_SEED_FILE", "")
if seed_path:
    Path(seed_path).write_text(
        json.dumps(markers, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
print(f"seeded: {json.dumps(markers, sort_keys=True)}")  # noqa: T201 - piped stdout
