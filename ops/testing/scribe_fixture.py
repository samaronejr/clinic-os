"""Seed recording-only consent through public services in the synthetic browser DB."""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from typing import Final
from uuid import UUID, uuid4

import django
from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.db import connection, transaction
from django.utils import timezone

MAX_SINK_VALUES: Final = 16
MAX_SINK_VALUE_LENGTH: Final = 4096


def inspect_sinks(*, organization: UUID, clinic: UUID) -> int:
    """Inspect technical rows without publishing their contents or sentinels."""
    from apps.audit.models import AuditEvent  # noqa: PLC0415
    from apps.comms.models import IntegrationOperation  # noqa: PLC0415

    sensitive: object = json.loads(os.environ["SCRIBE_FIXTURE_SENSITIVE"])
    if (
        not isinstance(sensitive, list)
        or not 1 <= len(sensitive) <= MAX_SINK_VALUES
        or not all(
            isinstance(value, str) and 1 <= len(value) <= MAX_SINK_VALUE_LENGTH
            for value in sensitive
        )
    ):
        return 2
    with transaction.atomic(durable=True), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [str(organization)]
        )
        audit = list(AuditEvent.objects.filter(organization_id=organization).values())
        outbox = list(
            IntegrationOperation.objects.filter(
                organization_id=organization, clinic_id=clinic
            ).values()
        )
    clean = {
        name: all(value not in json.dumps(records, default=str) for value in sensitive)
        for name, records in (("audit", audit), ("outbox", outbox))
    }
    sys.stdout.write(
        json.dumps(
            {
                "clean": clean,
                "rows": {"audit": len(audit), "outbox": len(outbox)},
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


def stored_action(
    *,
    action: str,
    organization: UUID,
    clinic: UUID,
    physician: UUID,
) -> int:
    """Inspect non-identifying capture terms or withdraw its real patient grant."""
    if action == "sinks":
        return inspect_sinks(organization=organization, clinic=clinic)
    from apps.consent.services import revoke_consent  # noqa: PLC0415
    from apps.intake.patient_access import patient_session_context  # noqa: PLC0415
    from apps.scribe.contracts import BUFFER_MS, SLICE_MS  # noqa: PLC0415
    from apps.scribe.models import (  # noqa: PLC0415
        AudioChunk,
        CaptureEpoch,
        RecordingSession,
    )

    with transaction.atomic(durable=True), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [str(organization)]
        )
        recording = RecordingSession.objects.get(
            pk=UUID(os.environ["SCRIBE_FIXTURE_SESSION"]),
            organization_id=organization,
            clinic_id=clinic,
            physician_id=physician,
        )
        patient = recording.consent.patient_session_id
        acceptance = recording.consent_id
        chunks = AudioChunk.objects.filter(session=recording)
        metadata = {
            "state": recording.state,
            "discarded": recording.discarded,
            "chunks": chunks.count(),
            "retained_keys": sum(
                bool(value) for value in chunks.values_list("wrapped_key", flat=True)
            ),
            "sequences": list(
                chunks.order_by("epoch__number", "seq").values_list(
                    "epoch__number", "seq", "state"
                )
            ),
            "epochs": [
                {
                    "number": epoch.number,
                    "closed": epoch.ended_at is not None,
                    "gaps": epoch.gaps,
                }
                for epoch in CaptureEpoch.objects.filter(session=recording).order_by(
                    "number"
                )
            ],
            "original_expiry_verified": all(
                expires == started + timedelta(milliseconds=BUFFER_MS + seq * SLICE_MS)
                for expires, started, seq in chunks.values_list(
                    "expires_at", "epoch__started_at", "seq"
                )
            ),
        }
    if action == "inspect":
        sys.stdout.write(json.dumps(metadata, sort_keys=True) + "\n")
        return 0
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        with patient_session_context(patient):
            revoke_consent(acceptance_id=acceptance)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
    sys.stdout.write("SCRIBE_FIXTURE_REVOKED\n")
    return 0


def main() -> int:
    """Require the synthetic owner boundary; publish no fixture identifiers."""
    if resolved_data_mode() != SYNTHETIC_DATA_MODE:
        return 2
    django.setup()
    from apps.consent.services import (  # noqa: PLC0415 - after Django setup
        prepare_acceptance,
        publish_text,
        record_consent,
        record_refusal,
    )
    from apps.identity.models import (  # noqa: PLC0415
        Clinic,
        ProfessionalRegistration,
        User,
        UserClinicRole,
    )
    from apps.intake.patient_access import (  # noqa: PLC0415
        issue_invitation,
        patient_session_context,
        redeem_invitation,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone() != ("clinic_owner",):
            return 2
    organization = UUID(os.environ["SCRIBE_FIXTURE_ORGANIZATION"])
    clinic = UUID(os.environ["SCRIBE_FIXTURE_CLINIC"])
    physician = UUID(os.environ["SCRIBE_FIXTURE_PHYSICIAN"])
    action = os.environ.get("SCRIBE_FIXTURE_ACTION", "consent")
    if action in {"inspect", "revoke", "sinks"}:
        return stored_action(
            action=action,
            organization=organization,
            clinic=clinic,
            physician=physician,
        )
    if action != "consent":
        return 2
    enrollment = UUID(os.environ["SCRIBE_FIXTURE_ENROLLMENT"])
    # Match the existing browser owner-fixture setup posture, never clinic_app
    # query-around-RLS. Public services below use a real scoped actor.
    with transaction.atomic(durable=True), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [str(organization)]
        )
        scoped_clinic = Clinic.objects.get(pk=clinic, organization_id=organization)
        owner = User.objects.create_user(
            username=f"scribe-fixture-{uuid4().hex}", password=None
        )
        UserClinicRole.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            user=owner,
            role="clinic_admin",
        )
        ProfessionalRegistration.objects.get_or_create(
            organization_id=organization,
            clinic_id=clinic,
            user_id=physician,
            role="physician",
            council="CRM",
            number="SINTETICO-040",
            jurisdiction=scoped_clinic.crm_uf,
            specialty="Sintetico",
            defaults={
                "status": "regular",
                "valid_from": timezone.now() - timedelta(days=1),
                "valid_to": timezone.now() + timedelta(days=1),
            },
        )
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        with tenant_context(owner.pk, organization):
            text = publish_text(
                clinic_id=clinic,
                purpose="consultation_recording",
                text="SINTETICO-40: autorização específica para gravar esta consulta.",
            )
            invitation = issue_invitation(clinic_id=clinic, enrollment_id=enrollment)
        patient = redeem_invitation(clinic, invitation.secret)
        if patient is None:
            return 2
        with patient_session_context(patient):
            _text, offer = prepare_acceptance(text_id=text.pk)
            if os.environ.get("SCRIBE_FIXTURE_ACCEPTED", "1") == "1":
                record_consent(
                    offer=offer, purpose="consultation_recording", accepted=True
                )
            else:
                record_refusal(offer=offer, purpose="consultation_recording")
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
    sys.stdout.write("SCRIBE_FIXTURE_READY\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
