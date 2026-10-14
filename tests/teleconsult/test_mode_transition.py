"""Immutable mode receipts preserve both participant channels and real toggles."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Literal, assert_never

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.intake.patient_access import patient_session_context
from apps.teleconsult import participants, services
from apps.teleconsult.models import TeleconsultEvent
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from patient_service_support import runtime_role
from renewal.test_encounters import setup_context
from renewal.test_retention import staff_client
from renewal.test_self_booking import _client
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_mode_audit import MODE_EVENTS, second_patient_session
from teleconsult.test_participants import World, seed_world

if TYPE_CHECKING:
    from uuid import UUID

    from django.test import Client

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)
type Role = Literal["patient", "physician"]
type EntryPoint = Literal["service", "sql", "http"]


def sql_mode(w: World, role: Role, *, enabled: bool) -> tuple[int, int] | None:
    """Insert through SQL and return the actual transition/receipt, or no-op."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO clinic_app.teleconsult_teleconsultevent "
            "(organization_id,session_id,kind,actor_role,reason_code,created_at) "
            "VALUES (%s,%s,%s,%s,'',%s) RETURNING id,mode_audit_seq",
            [
                str(w.organization),
                str(w.session),
                "audio_only" if enabled else "video_restored",
                role,
                timezone.now(),
            ],
        )
        row = cursor.fetchone()
    return None if row is None else (int(row[0]), int(row[1]))


def mode_history(w: World) -> tuple[list[TeleconsultEvent], list[AuditEvent]]:
    with setup_context(w.organization):
        events = list(
            TeleconsultEvent.objects.filter(
                session_id=w.session, kind__in=("audio_only", "video_restored")
            ).order_by("mode_audit_seq")
        )
        audits = list(
            AuditEvent.objects.filter(
                organization_id=w.organization,
                affected_record_id=str(w.session),
                event_type__in=MODE_EVENTS,
            ).order_by("seq")
        )
        verify_chain(w.organization)
    return events, audits


@pytest.mark.parametrize("entrypoint", ["service", "sql", "http"])
def test_two_patient_sessions_and_physician_keep_independent_mode_receipts(
    rbac_graph: RbacGraph, synthetic_provider: object, entrypoint: EntryPoint
) -> None:
    w = seed_world(rbac_graph)
    second = second_patient_session(w)
    actions: list[tuple[Role, UUID, bool]] = [
        ("patient", w.patient_session, True),
        ("patient", second, True),
        ("physician", w.graph.physician, True),
        ("patient", second, False),
        ("physician", w.graph.physician, True),
        ("patient", w.patient_session, False),
        ("patient", second, True),
        ("physician", w.graph.physician, False),
        ("patient", w.patient_session, False),
        ("patient", second, False),
        ("physician", w.graph.physician, True),
        ("patient", second, True),
        ("patient", w.patient_session, True),
        ("physician", w.graph.physician, True),
        ("patient", w.patient_session, False),
        ("physician", w.graph.physician, False),
    ]
    clients: dict[UUID, Client] = {}
    with ExitStack() as resources:
        match entrypoint:
            case "http":
                clients[w.patient_session] = _client(w.patient_session)
                clients[second] = _client(second)
                clients[w.graph.physician] = resources.enter_context(
                    staff_client(w.graph.physician)
                )
            case "service" | "sql":
                clients = {}
            case unreachable:
                assert_never(unreachable)
        for role, actor, enabled in actions:
            match entrypoint:
                case "http":
                    url = (
                        "/patient/teleconsult/"
                        if role == "patient"
                        else f"/teleconsult/clinics/{w.clinic}/"
                    )
                    response = clients[actor].post(
                        url,
                        {
                            "action": "audio_only",
                            "session_id": str(w.session),
                            "enabled": "true" if enabled else "false",
                        },
                        HTTP_ACCEPT="application/json",
                    )
                    assert response.status_code == 200
                    assert response.json() == {"audio_only": enabled}
                case "service" | "sql":
                    authority = (
                        patient_session_context(actor)
                        if role == "patient"
                        else tenant_context(actor, w.organization)
                    )
                    with runtime_role(), authority:
                        if entrypoint == "sql":
                            sql_mode(w, role, enabled=enabled)
                        elif role == "patient":
                            participants.set_patient_audio_only(
                                session_id=w.session, enabled=enabled
                            )
                        else:
                            participants.set_audio_only(
                                clinic_id=w.clinic,
                                session_id=w.session,
                                enabled=enabled,
                            )
                case unreachable:
                    assert_never(unreachable)
    expected: list[tuple[Role, UUID, str]] = [
        ("patient", w.patient_session, "audio_only"),
        ("physician", w.graph.physician, "audio_only"),
        ("patient", second, "video_restored"),
        ("patient", second, "audio_only"),
        ("physician", w.graph.physician, "video_restored"),
        ("patient", w.patient_session, "video_restored"),
        ("physician", w.graph.physician, "audio_only"),
        ("patient", second, "audio_only"),
        ("patient", w.patient_session, "video_restored"),
        ("physician", w.graph.physician, "video_restored"),
    ]
    events, audits = mode_history(w)
    assert len(events) == len(audits) == len(expected)
    for event, audit, (role, actor, kind) in zip(events, audits, expected, strict=True):
        assert (event.actor_role, event.kind, event.mode_audit_seq) == (
            role,
            kind,
            audit.seq,
        )
        assert audit.actor_user_id == actor
        assert audit.event_type == MODE_EVENTS[kind == "video_restored"]
        assert audit.payload == {"clinic_id": str(w.clinic), "object_verb": kind}
        assert audit.affected_record_type == "teleconsult.session"
        assert audit.affected_record_id == str(w.session)
        assert audit.component_id == "clinic-os-web"
        assert audit.component_ip is None


@pytest.mark.parametrize("role", ["patient", "physician"])
def test_same_statement_toggles_have_distinct_canonical_receipts(
    rbac_graph: RbacGraph, synthetic_provider: object, role: Role
) -> None:
    w = seed_world(rbac_graph)
    authority = (
        patient_session_context(w.patient_session)
        if role == "patient"
        else tenant_context(w.graph.physician, w.organization)
    )
    with runtime_role(), authority, connection.cursor() as cursor:
        cursor.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
        cursor.execute(
            "INSERT INTO clinic_app.teleconsult_teleconsultevent "
            "(organization_id,session_id,kind,actor_role,reason_code,created_at) "
            "SELECT %s,%s,v.kind,%s,'',%s FROM (VALUES "
            "('audio_only'),('video_restored'),('audio_only'),('video_restored')) "
            "v(kind) RETURNING id,mode_audit_seq,created_at",
            [str(w.organization), str(w.session), role, timezone.now()],
        )
        rows = cursor.fetchall()
    assert len(rows) == 4
    assert len({row[1] for row in rows}) == 4
    assert len({row[2] for row in rows}) == 1
    events, audits = mode_history(w)
    assert [event.kind for event in events] == [
        "audio_only",
        "video_restored",
        "audio_only",
        "video_restored",
    ]
    assert len(audits) == 4


def test_second_valid_session_cannot_supply_or_rebind_a_receipt(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    second = second_patient_session(w)
    with runtime_role(), patient_session_context(w.patient_session):
        sql_mode(w, "patient", enabled=True)
    events, before = mode_history(w)
    with runtime_role(), patient_session_context(second):
        with pytest.raises(DatabaseError) as caught, transaction.atomic():
            TeleconsultEvent.objects.create(
                organization_id=w.organization,
                session_id=w.session,
                kind="video_restored",
                actor_role="patient",
                mode_audit_seq=before[0].seq,
            )
        cause = caught.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert cause.sqlstate == "42501"
        with pytest.raises(DatabaseError), transaction.atomic():
            connection.cursor().execute(
                "UPDATE clinic_app.teleconsult_teleconsultevent "
                "SET mode_audit_seq=NULL WHERE id=%s",
                [events[0].pk],
            )
    after_events, after = mode_history(w)
    assert [event.pk for event in after_events] == [events[0].pk]
    assert [audit.seq for audit in after] == [before[0].seq]
    with runtime_role(), patient_session_context(second):
        sql_mode(w, "patient", enabled=False)
    _, final = mode_history(w)
    assert [audit.actor_user_id for audit in final] == [w.patient_session, second]


@pytest.mark.parametrize("role", ["patient", "physician"])
def test_toggle_after_the_end_answers_a_json_conflict(
    rbac_graph: RbacGraph, synthetic_provider: object, role: Role
) -> None:
    w = seed_world(rbac_graph)
    with runtime_role(), tenant_context(w.graph.physician, w.organization):
        services.end_consultation(clinic_id=w.clinic, session_id=w.session)
    with ExitStack() as resources:
        client = (
            _client(w.patient_session)
            if role == "patient"
            else resources.enter_context(staff_client(w.graph.physician))
        )
        response = client.post(
            "/patient/teleconsult/"
            if role == "patient"
            else f"/teleconsult/clinics/{w.clinic}/",
            {"action": "audio_only", "session_id": str(w.session), "enabled": "true"},
            HTTP_ACCEPT="application/json",
        )
    assert response.status_code == 409
    assert response.json() == {"code": "session_closed"}
    events, audits = mode_history(w)
    assert events == []
    assert audits == []
