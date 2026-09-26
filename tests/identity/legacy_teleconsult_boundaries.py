"""Stored teleconsult and worker scopes; only the transport handoff is captured."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.comms.adapters import OperationScope
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation
from apps.consent.models import ConsentText
from apps.core import integration
from apps.ehr.services import open_encounter
from apps.scheduling.services import AppointmentLocalRange, create_appointment
from apps.teleconsult import services, workspace
from apps.teleconsult.adapters import PROVIDER, SyntheticRoomAdapter
from apps.teleconsult.models import (
    TeleconsultCredential,
    TeleconsultRoom,
    TeleconsultSession,
)
from apps.tenancy.db import tenant_context
from django.db import connection

from identity.legacy_parity_support import LEGACY, PHYSICIAN, Boundary, has_rows
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context

if TYPE_CHECKING:
    import pytest

    from identity.legacy_operational_boundaries import OperationalSubjects
    from identity.legacy_parity_support import LegacyWorld


@dataclass(frozen=True)
class TeleconsultSubjects:
    session: TeleconsultSession
    credential: TeleconsultCredential
    operation: IntegrationOperation


def seed_teleconsult(
    w: LegacyWorld, op: OperationalSubjects, monkeypatch: pytest.MonkeyPatch
) -> TeleconsultSubjects:
    dispatched: list[object] = []
    monkeypatch.setattr(
        execute_operation, "apply_async", lambda **kwargs: dispatched.append(kwargs)
    )
    integration.register_send_adapter(SyntheticRoomAdapter())
    with runtime_role(), tenant_context(w.graph.physician, w.graph.organization_a):
        session = services.create_session(clinic_id=w.clinic, encounter_id=w.encounter)
    assert len(dispatched) == 1
    with setup_context(w.graph.organization_a):
        room = TeleconsultRoom.objects.get(session=session)
    with runtime_role():
        assert (
            execute_operation.apply(
                kwargs={"operation_id": str(room.operation_id)}
            ).result
            == "succeeded"
        )
    with runtime_role(), tenant_context(w.graph.physician, w.graph.organization_a):
        join = services.request_physician_join(
            clinic_id=w.clinic, session_id=session.pk
        )
        credential = TeleconsultCredential.objects.select_related("session").get(
            token_digest=hashlib.sha256(join.token.encode()).hexdigest()
        )
        operation = IntegrationOperation.objects.get(pk=room.operation_id)
    with runtime_role(), tenant_context(w.graph.shared_user, w.graph.organization_a):
        appointment = create_appointment(
            clinic_id=w.clinic,
            enrollment_id=op.enrollment,
            practitioner_id=w.graph.physician,
            local_range=AppointmentLocalRange("2035-06-02T10:00", "2035-06-02T10:30"),
            idempotency_key=uuid4(),
        )
    with runtime_role(), tenant_context(w.graph.physician, w.graph.organization_a):
        open_encounter(clinic_id=w.clinic, appointment_id=appointment.pk)
    return TeleconsultSubjects(session, credential, operation)


def _assigned(w: LegacyWorld, ok: bool, data: TeleconsultSubjects) -> object:
    original = data.session.physician_id
    if not ok:
        data.session.physician_id = uuid4()
    try:
        return services._assigned_physician(data.session)
    finally:
        data.session.physician_id = original


def _enter(w: LegacyWorld, ok: bool, data: TeleconsultSubjects) -> object:
    original = data.credential.participant_id
    if not ok:
        data.credential.participant_id = uuid4()
    try:
        services._enter_as_physician(data.credential)
        return None
    finally:
        data.credential.participant_id = original


def _terminal(w: LegacyWorld, ok: bool, data: TeleconsultSubjects) -> object:
    """Whether ``_fail`` really ended the session.

    A newer consent text is a stored ``consent_revoked`` failure; the
    transition then succeeds only for the session's own physician. The
    invalid call claims ``encounter_closed`` while the encounter is open.
    """
    _publish_newer_text(w)
    services._fail(data.session.pk, "consent_revoked" if ok else "encounter_closed")
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
        cursor.execute(
            "SELECT state FROM clinic_app.teleconsult_teleconsultsession WHERE id = %s",
            [data.session.pk],
        )
        row = cursor.fetchone()
        cursor.execute("SET LOCAL ROLE clinic_app")
    return row == ("failed",)


def _publish_newer_text(w: LegacyWorld) -> None:
    """Publish a newer text as its publisher, then restore the actor."""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
        text = ConsentText.objects.filter(clinic_id=w.clinic).latest("version")
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(text.published_by_id)],
        )
        ConsentText.objects.create(
            organization_id=text.organization_id,
            clinic_id=text.clinic_id,
            purpose=text.purpose,
            version=text.version + 1,
            text="Sintetico nova",
            language=text.language,
            digest=hashlib.sha256(b"Sintetico nova").hexdigest(),
            published_by_id=text.published_by_id,
        )
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(w.actor.pk)],
        )
        cursor.execute("SET LOCAL ROLE clinic_app")


def boundaries(data: TeleconsultSubjects) -> tuple[Boundary, ...]:
    return (
        Boundary(
            "apps.teleconsult.services._assigned_physician",
            "teleconsult",
            PHYSICIAN,
            lambda w, ok: _assigned(w, ok, data),
        ),
        Boundary(
            "apps.teleconsult.services._fail",
            "teleconsult",
            PHYSICIAN,
            lambda w, ok: _terminal(w, ok, data),
            decision=lambda result: result is True,
        ),
        Boundary(
            "apps.teleconsult.services._staff_session",
            "teleconsult",
            LEGACY,
            lambda w, ok: services._staff_session(w.clinic_for(ok), data.session.pk),
        ),
        Boundary(
            "apps.teleconsult.services.create_session",
            "teleconsult",
            PHYSICIAN,
            lambda w, ok: services.create_session(
                clinic_id=w.clinic_for(ok), encounter_id=w.encounter
            ),
        ),
        Boundary(
            "apps.teleconsult.services._enter_as_physician",
            "teleconsult",
            PHYSICIAN,
            lambda w, ok: _enter(w, ok, data),
        ),
        Boundary(
            "apps.teleconsult.services.staff_sessions",
            "teleconsult",
            PHYSICIAN,
            lambda w, ok: services.staff_sessions(clinic_id=w.clinic_for(ok)),
            has_rows,
        ),
        Boundary(
            "apps.teleconsult.services.open_encounters",
            "teleconsult",
            PHYSICIAN,
            lambda w, ok: services.open_encounters(clinic_id=w.clinic_for(ok)),
            has_rows,
        ),
        Boundary(
            "apps.teleconsult.services._session_scope",
            "teleconsult",
            LEGACY,
            lambda w, ok: services._session_scope(data.session.pk if ok else uuid4()),
            has_rows,
        ),
        Boundary(
            "apps.teleconsult.services._room_state",
            "teleconsult",
            LEGACY,
            lambda w, ok: services._room_state(data.session.pk if ok else uuid4()),
            lambda result: result != "none",
        ),
        Boundary(
            "apps.teleconsult.workspace._session_version",
            "teleconsult",
            PHYSICIAN,
            lambda w, ok: workspace._session_version(
                w.clinic_for(ok), data.session, str(w.version.pk)
            ),
        ),
        Boundary(
            "apps.core.integration._prepare_with_authority",
            "worker_authority",
            LEGACY,
            lambda w, ok: integration._prepare_with_authority(
                OperationScope(
                    operation_id=data.operation.pk,
                    organization_id=w.graph.organization_a,
                    clinic_id=w.clinic,
                    actor_id=w.actor.pk if ok else uuid4(),
                ),
                SyntheticRoomAdapter(),
            ),
        ),
        Boundary(
            "apps.core.integration._resolve_scope",
            "worker_resolver",
            LEGACY,
            lambda w, ok: integration._resolve_scope(
                data.operation.pk if ok else uuid4()
            ),
            has_rows,
        ),
        Boundary(
            "apps.core.integration._resolve_callback_scope",
            "worker_resolver",
            LEGACY,
            lambda w, ok: integration._resolve_callback_scope(
                PROVIDER,
                str(data.operation.provider_reference) if ok else "SINTETICO-UNKNOWN",
            ),
            has_rows,
        ),
    )
