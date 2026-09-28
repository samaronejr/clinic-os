"""Teleconsult v2 participant guards: census, record scope, refusals, behaviour.

The new staff guards are derived from ``apps/teleconsult`` source: exactly one
function calls ``require_permission`` (``clinician_session``) and every public
caller of it is in ``GUARDS``. The permission names are this module's own
literals. Each cell records the exact arguments reaching
``clinic_app.has_permission`` (a statement spy), the refusal, the rows written
and the realtime hints scheduled; a refusal writes and schedules nothing. The
database half proves the same decision in ``teleconsult_clinician``, the
device-check trigger and RLS, including direct ``clinic_app`` writes.
"""

from __future__ import annotations

import ast
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation
from apps.ehr.models import SpecialtyTemplate
from apps.ehr.services import create_draft
from apps.identity.models import RoleGrant, User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import patient_session_context
from apps.realtime import authorization as realtime_authorization
from apps.realtime.topics import TOPIC_PATTERN
from apps.teleconsult import hints, participants
from apps.teleconsult.models import (
    TeleconsultCredential,
    TeleconsultDeviceCheck,
    TeleconsultRoom,
    TeleconsultSession,
)
from apps.teleconsult.participants import DeviceResults
from apps.teleconsult.services import (
    TeleconsultAccessDeniedError,
    TeleconsultConflictError,
    enter_room,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from consent.test_authority import (
    World as ConsentWorld,
)
from consent.test_authority import (
    _bind,
    _normalized,
    _row_counts,
    _writes,
    as_owner,
    member,
    permitted,
)
from consent.test_purposes import _patient_session, register_physician
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context
from renewal.test_retention import staff_client
from renewal.test_teleconsult_sessions import (
    _create,
    _kinds,
    _operation,
    _patient_enter,
    _patient_join,
    _physician_enter,
    _physician_join,
    _run_room,
    room_name,
    seed,
    synthetic_provider,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from django.test import Client
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]
ROLES = tuple(UserClinicRole.Role.values)
CLINICAL = ("clinical.write",)
DIRECT = {"apps.teleconsult.participants.clinician_session"}
GUARDS: dict[str, tuple[str, ...]] = {
    "apps.teleconsult.participants.record_device_check": CLINICAL,
    "apps.teleconsult.participants.set_audio_only": CLINICAL,
    "apps.teleconsult.participants.resume_physician": CLINICAL,
    "apps.teleconsult.participants.remove_patient": CLINICAL,
    "apps.teleconsult.participants.authorize_room_topic": CLINICAL,
}
# The write-then-render view helper decides the same guard before any write;
# the view matrix below covers it in every reply mode.
VIEW_GUARDS = {"apps.teleconsult.views._participant_action"}
MUTATING = {
    "apps.teleconsult.participants.record_device_check",
    "apps.teleconsult.participants.set_audio_only",
    "apps.teleconsult.participants.resume_physician",
    "apps.teleconsult.participants.remove_patient",
}
RESULTS = DeviceResults("ok", "ok", "ok", "good")
OPAQUE = re.compile(r"tc-[0-9a-f]{32}")


def _functions_calling(name: str) -> set[str]:
    found: set[str] = set()
    for path in sorted((ROOT / "apps/teleconsult").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        module = str(path.relative_to(ROOT))[:-3].replace("/", ".")
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(call, ast.Call)
                and (
                    (isinstance(call.func, ast.Name) and call.func.id == name)
                    or (isinstance(call.func, ast.Attribute) and call.func.attr == name)
                )
                for call in ast.walk(node)
            ):
                found.add(f"{module}.{node.name}")
    return found


@dataclass(frozen=True)
class World:
    graph: RbacGraph
    session: UUID
    enrollment: UUID
    patient_session: UUID
    room: str
    physician_token: str
    patient_token: str

    @property
    def clinic(self) -> UUID:
        return self.graph.clinic_a

    @property
    def organization(self) -> UUID:
        return self.graph.organization_a

    def consent_world(self) -> ConsentWorld:
        return ConsentWorld(self.graph, uuid4(), self.enrollment, uuid4())


def seed_world(graph: RbacGraph) -> World:
    """A provisioned session both participants entered; the physician is registered."""
    appointment, encounter, _, patient_session, _ = seed(graph)
    register_physician(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    physician_token = _physician_join(graph, session)
    _physician_enter(graph, physician_token)
    patient_token = _patient_join(patient_session, session)
    _patient_enter(patient_session, patient_token)
    with setup_context(graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        ).pk
    return World(
        graph,
        session.pk,
        enrollment,
        patient_session,
        room_name(graph, session),
        physician_token,
        patient_token,
    )


def calls(w: World, clinic: UUID, session: UUID) -> dict[str, Callable[[], object]]:
    return {
        "apps.teleconsult.participants.record_device_check": lambda: (
            participants.record_device_check(
                clinic_id=clinic, session_id=session, results=RESULTS
            )
        ),
        "apps.teleconsult.participants.set_audio_only": lambda: (
            participants.set_audio_only(
                clinic_id=clinic, session_id=session, enabled=True
            )
        ),
        "apps.teleconsult.participants.resume_physician": lambda: (
            participants.resume_physician(clinic_id=clinic, session_id=session)
        ),
        "apps.teleconsult.participants.remove_patient": lambda: (
            participants.remove_patient(clinic_id=clinic, session_id=session)
        ),
        "apps.teleconsult.participants.authorize_room_topic": lambda: (
            participants.authorize_room_topic(room_name=w.room)
        ),
    }


@dataclass
class Cell:
    error: Exception | None = None
    names: list[tuple[object, ...]] = field(default_factory=list)
    writes: int = 0
    hints: list[str] = field(default_factory=list)


@pytest.fixture
def hint_spy(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every hint scheduled by the teleconsult domain."""
    settings.REALTIME_ENABLED = True
    scheduled: list[str] = []
    monkeypatch.setattr(
        hints,
        "publish_on_commit",
        lambda *, topic, kind, version: scheduled.append(f"{topic}:{kind}:{version}"),
    )
    return scheduled


def run(
    actor: UUID, organization: UUID, call: Callable[[], object], spy: list[str]
) -> Cell:
    """Execute one cell in a rolled-back savepoint and observe it."""
    cell = Cell()

    def statements(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object],
        many: bool,
        context: object,
    ) -> object:
        if "clinic_app.has_permission(" in sql:
            cell.names.append(tuple(params))
        return execute(sql, params, many, context)

    _bind(actor, organization)
    before = _writes()
    spy.clear()
    try:
        with transaction.atomic(), connection.execute_wrapper(statements):
            call()
            transaction.set_rollback(True)
    except (TeleconsultAccessDeniedError, DatabaseError) as error:
        cell.error = error
    cell.writes = _writes() - before
    cell.hints = list(spy)
    return cell


@contextmanager
def matrix_transaction(w: World) -> Iterator[None]:
    with runtime_role(), transaction.atomic():
        _bind(w.graph.physician, w.organization)
        yield
        transaction.set_rollback(True)


def expected(w: World) -> list[tuple[object, ...]]:
    return [(name, w.clinic, w.enrollment) for name in CLINICAL]


def expected_for(w: World, actor: UUID) -> list[tuple[object, ...]]:
    """Names reaching has_permission: the guard asks only for a session it can see.

    The stored session row is read under the actor's own RLS, so an actor the
    session read policy hides is refused before any permission call.
    """
    _bind(actor, w.organization)
    with transaction.atomic():
        visible = TeleconsultSession.objects.filter(pk=w.session).exists()
        enrollment = (
            PatientClinicEnrollment.objects.filter(pk=w.enrollment)
            .values_list("pk", flat=True)
            .first()
        )
    return [(name, w.clinic, enrollment) for name in CLINICAL] if visible else []


def assert_refused(guard: str, cell: Cell) -> None:
    assert type(cell.error) is TeleconsultAccessDeniedError, (guard, cell.error)
    assert cell.error.args == (), guard
    assert cell.writes == 0, (guard, cell.writes)
    assert cell.hints == [], guard


def test_guards_are_derived_from_source_and_decide_through_has_permission() -> None:
    assert _functions_calling("require_permission") == DIRECT
    assert _functions_calling("clinician_session") == set(GUARDS) | VIEW_GUARDS
    assert set(BUNDLES_V1) == set(ROLES)
    allowed = permitted(CLINICAL)
    assert allowed == {"physician"}
    assert set(ROLES) - allowed
    source = (ROOT / "apps/teleconsult/participants.py").read_text()
    assert "require_current_actor_clinic_roles" not in source
    assert "questionnaire_staff" not in source
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_get_functiondef("
            "'clinic_app.teleconsult_clinician(uuid)'::regprocedure)"
        )
        row = cursor.fetchone()
        cursor.execute(
            "SELECT policyname, coalesce(qual,'') || ' ' || coalesce(with_check,'') "
            "FROM pg_catalog.pg_policies WHERE schemaname='clinic_app' "
            "AND tablename='teleconsult_teleconsultdevicecheck' "
            "AND 'clinic_app' = ANY(roles) ORDER BY 1"
        )
        policies = cursor.fetchall()
    assert row is not None
    assert re.findall(r"has_permission\('([a-z_.]+)'", str(row[0])) == list(CLINICAL)
    assert "questionnaire_staff" not in str(row[0])
    assert [name for name, _ in policies] == [
        "teleconsult_device_insert",
        "teleconsult_device_read",
    ]
    for _, body in policies:
        assert "teleconsult_clinician(session_id)" in body
        assert "questionnaire_staff" not in body


def test_every_catalog_role_and_unassigned_actor_is_refused(
    rbac_graph: RbacGraph, synthetic_provider: object, hint_spy: list[str]
) -> None:
    w = seed_world(rbac_graph)
    consent_world = w.consent_world()
    # Every catalog role with full professional scope on this very patient.
    actors = {
        role: member(consent_world, role, care_team=w.enrollment) for role in ROLES
    }
    nobody = User.objects.create(username=f"synthetic-tc-none-{uuid4().hex}").pk
    decisions = 0
    asked = set()
    with matrix_transaction(w):
        for role, actor in actors.items():
            names = expected_for(w, actor)
            for guard, call in calls(w, w.clinic, w.session).items():
                cell = run(actor, w.organization, call, hint_spy)
                decisions += 1
                # Every visible session reaches has_permission with its patient's
                # enrollment; the physician holds clinical.write and is refused
                # by the participant relation alone.
                assert cell.names == names, (guard, role)
                assert_refused(guard, cell)
            if names:
                asked.add(role)
        for guard, call in calls(w, w.clinic, w.session).items():
            cell = run(nobody, w.organization, call, hint_spy)
            decisions += 1
            assert cell.names == expected_for(w, nobody), guard
            assert_refused(guard, cell)
        # Positive control: the bound physician is admitted and really writes.
        for guard, call in calls(w, w.clinic, w.session).items():
            cell = run(w.graph.physician, w.organization, call, hint_spy)
            decisions += 1
            assert cell.error is None, (guard, cell.error)
            assert cell.names == expected(w), guard
            if guard in MUTATING:
                assert cell.writes > 0, guard
                assert cell.hints == [
                    f"teleconsult:{w.room}:teleconsult:1",
                    f"patient:{w.enrollment}:teleconsult:teleconsult:1",
                ], guard
    assert decisions == (len(ROLES) + 2) * len(GUARDS)
    # The physician cell is decided by the relation, not by a hidden session.
    assert "physician" in asked


def test_inactive_narrowed_unregistered_foreign_clinic_and_tenant_are_refused(
    rbac_graph: RbacGraph, synthetic_provider: object, hint_spy: list[str]
) -> None:
    w = seed_world(rbac_graph)
    graph = w.graph
    physician = graph.physician
    # The bound physician also works in clinic B, with a registration there.
    with owner_context(w.organization):
        UserClinicRole.objects.create(
            organization_id=w.organization,
            clinic_id=graph.clinic_b,
            user_id=physician,
            role="physician",
        )
    with matrix_transaction(w):
        for guard in GUARDS:
            # Inactive: the actor accessor refuses before any permission call.
            _bind(physician, w.organization)
            with transaction.atomic():
                with as_owner():
                    User.objects.filter(pk=physician).update(is_active=False)
                cell = run(
                    physician,
                    w.organization,
                    calls(w, w.clinic, w.session)[guard],
                    hint_spy,
                )
                transaction.set_rollback(True)
            assert cell.names == [], guard
            assert_refused(guard, cell)
            # clinical.write removed from the physician bundle in clinic A.
            _bind(physician, w.organization)
            with transaction.atomic():
                with as_owner():
                    RoleGrant.objects.create(
                        organization_id=w.organization,
                        clinic_id=w.clinic,
                        role="physician",
                        permission="clinical.write",
                        valid_from=timezone.now() - timedelta(days=1),
                    )
                cell = run(
                    physician,
                    w.organization,
                    calls(w, w.clinic, w.session)[guard],
                    hint_spy,
                )
                transaction.set_rollback(True)
            assert cell.names == expected(w), guard
            assert_refused(guard, cell)
            # A revoked professional registration: no clinical permission.
            _bind(physician, w.organization)
            with transaction.atomic():
                with as_owner():
                    connection.cursor().execute(
                        "UPDATE clinic_app.identity_professionalregistration "
                        "SET revoked_at=statement_timestamp() WHERE user_id=%s",
                        [str(physician)],
                    )
                cell = run(
                    physician,
                    w.organization,
                    calls(w, w.clinic, w.session)[guard],
                    hint_spy,
                )
                transaction.set_rollback(True)
            assert cell.names == expected(w), guard
            assert_refused(guard, cell)
            if guard.endswith("authorize_room_topic"):
                continue
            # Clinic scope: naming clinic B (where the actor works) for an A session.
            cell = run(
                physician,
                w.organization,
                calls(w, graph.clinic_b, w.session)[guard],
                hint_spy,
            )
            assert cell.names == [], guard
            assert_refused(guard, cell)
            # An unknown session: the same refusal, nothing asked.
            cell = run(
                physician, w.organization, calls(w, w.clinic, uuid4())[guard], hint_spy
            )
            assert cell.names == [], guard
            assert_refused(guard, cell)
            # Another tenant: the stored session is invisible under RLS.
            cell = run(
                physician,
                graph.organization_b,
                calls(w, w.clinic, w.session)[guard],
                hint_spy,
            )
            assert cell.names == [], guard
            assert_refused(guard, cell)


def test_database_participant_scope_both_ways_including_direct_writes(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    graph = w.graph
    colleague = member(w.consent_world(), "physician", care_team=w.enrollment)
    with setup_context(w.organization):
        patient = TeleconsultRoom.objects.get(session_id=w.session).session.patient_id

    def decide(actor: UUID, organization: UUID) -> object:
        with (
            runtime_role(),
            tenant_context(actor, organization),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.teleconsult_clinician(%s)", [str(w.session)]
            )
            return cursor.fetchone()

    def insert(role: str, participant: UUID) -> str | None:
        try:
            with transaction.atomic():
                TeleconsultDeviceCheck.objects.create(
                    organization_id=w.organization,
                    session_id=w.session,
                    role=role,
                    participant_id=participant,
                    camera="ok",
                    microphone="ok",
                    speaker="ok",
                    network="good",
                )
        except DatabaseError as error:
            return str(error)
        return None

    assert decide(graph.physician, w.organization) == (True,)
    assert decide(colleague, w.organization) == (False,)
    with (
        runtime_role(),
        tenant_context(graph.shared_user, w.organization),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT clinic_app.teleconsult_clinician(%s)", [str(uuid4())])
        assert cursor.fetchone() == (False,)
    # Direct clinic_app writes: the bound physician for itself only.
    with runtime_role(), tenant_context(graph.physician, w.organization):
        assert insert("physician", graph.physician) is None
        assert "invalid teleconsult device check" in str(insert("physician", colleague))
        assert "participant authority" in str(insert("patient", patient))
    with runtime_role(), tenant_context(colleague, w.organization):
        assert "participant authority" in str(insert("physician", graph.physician))
        assert "participant authority" in str(insert("physician", colleague))
        assert TeleconsultDeviceCheck.objects.count() == 0
    with runtime_role(), patient_session_context(w.patient_session):
        assert insert("patient", patient) is None
        assert "participant authority" in str(insert("physician", graph.physician))
        assert "invalid teleconsult device check" in str(insert("patient", uuid4()))
        # The patient reads only its own rows.
        assert set(TeleconsultDeviceCheck.objects.values_list("role", flat=True)) == {
            "patient"
        }
    with runtime_role(), tenant_context(graph.physician, w.organization):
        assert TeleconsultDeviceCheck.objects.count() == 2
    # Owner writes still pass the trigger's participant authority.
    with setup_context(w.organization):
        assert "participant authority" in str(insert("physician", graph.physician))
    # The bound physician without clinical.write: SQL decides like Python.
    with setup_context(w.organization):
        RoleGrant.objects.create(
            organization_id=w.organization,
            clinic_id=w.clinic,
            role="physician",
            permission="clinical.write",
            valid_from=timezone.now() - timedelta(days=1),
        )
    assert decide(graph.physician, w.organization) == (False,)
    with runtime_role(), tenant_context(graph.physician, w.organization):
        assert "participant authority" in str(insert("physician", graph.physician))


def test_staff_refusals_equal_the_unknown_clinic_refusal_and_change_nothing(
    rbac_graph: RbacGraph, synthetic_provider: object, superuser_database_url: str
) -> None:
    w = seed_world(rbac_graph)
    colleague = member(w.consent_world(), "physician", care_team=w.enrollment)
    receptionist = member(w.consent_world(), "receptionist")
    session = str(w.session)
    device = {"camera": "ok", "microphone": "ok", "speaker": "ok", "network": "good"}
    cases: list[tuple[str, UUID, dict[str, str]]] = [
        (
            "colleague-device",
            colleague,
            {"action": "device_check", "session_id": session, **device},
        ),
        (
            "colleague-audio",
            colleague,
            {"action": "audio_only", "session_id": session, "enabled": "true"},
        ),
        ("colleague-remove", colleague, {"action": "remove", "session_id": session}),
        ("colleague-resume", colleague, {"action": "resume", "session_id": session}),
        ("reception-remove", receptionist, {"action": "remove", "session_id": session}),
        (
            "unknown-session",
            w.graph.physician,
            {"action": "remove", "session_id": str(uuid4())},
        ),
        (
            "malformed-session",
            w.graph.physician,
            {"action": "resume", "session_id": "x"},
        ),
        (
            "forged-code",
            w.graph.physician,
            {
                "action": "device_check",
                "session_id": session,
                **device,
                "camera": "Sintetico",
            },
        ),
        (
            "forged-choice",
            w.graph.physician,
            {"action": "audio_only", "session_id": session, "enabled": "sim"},
        ),
        ("empty", w.graph.physician, {}),
    ]
    for name, actor, data in cases:
        with staff_client(actor) as client:
            unknown = f"/teleconsult/clinics/{uuid4()}/"
            assert client.post(unknown, data).status_code == 403
            reference = client.post(unknown, data)
            session_items = dict(client.session.items())
            key = client.session.session_key
            before = _row_counts(superuser_database_url)
            response = client.post(
                f"/teleconsult/clinics/{w.clinic}/",
                data,
                HTTP_ACCEPT="application/json",
            )
            assert response.status_code == 403, name
            assert _normalized(response.content) == _normalized(reference.content), name
            assert response.cookies.keys() == reference.cookies.keys() == set(), name
            assert client.session.session_key == key, name
            assert dict(client.session.items()) == session_items, name
            assert _row_counts(superuser_database_url) == before, name
    # Control: the bound physician's same request answers and writes.
    with staff_client(w.graph.physician) as client:
        before = _row_counts(superuser_database_url)
        response = client.post(
            f"/teleconsult/clinics/{w.clinic}/",
            {"action": "device_check", "session_id": session, **device},
            HTTP_ACCEPT="application/json",
        )
        assert response.status_code == 200
        assert response.json() == {"recorded": True}
        assert _row_counts(superuser_database_url) != before


def test_patient_actions_refuse_foreign_sessions_identically(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    graph = w.graph
    # A second patient's session in the same clinic.
    other = seed_world_other_patient(graph)
    with (
        runtime_role(),
        patient_session_context(other),
        pytest.raises(PatientAccessDeniedError) as unknown,
    ):
        participants.resume_patient(session_id=uuid4())
    for call in (
        lambda session: participants.record_patient_device_check(
            session_id=session, results=RESULTS
        ),
        lambda session: participants.set_patient_audio_only(
            session_id=session, enabled=True
        ),
        lambda session: participants.resume_patient(session_id=session),
    ):
        for session in (w.session, uuid4()):
            with runtime_role(), patient_session_context(other), transaction.atomic():
                before = _writes()
                with pytest.raises(PatientAccessDeniedError) as refused:
                    call(session)
                assert type(refused.value) is type(unknown.value)
                assert refused.value.args == unknown.value.args
                assert _writes() == before


def seed_world_other_patient(graph: RbacGraph) -> UUID:
    session, _ = _patient_session(graph)
    return session


def test_device_check_codes_audio_only_and_hints_after_commit(
    rbac_graph: RbacGraph, synthetic_provider: object, hint_spy: list[str]
) -> None:
    w = seed_world(rbac_graph)
    graph = w.graph
    hint_spy.clear()
    with runtime_role(), tenant_context(graph.physician, w.organization):
        check = participants.record_device_check(
            clinic_id=w.clinic, session_id=w.session, results=RESULTS
        )
        assert (check.role, check.participant_id) == ("physician", graph.physician)
        assert participants.set_audio_only(
            clinic_id=w.clinic, session_id=w.session, enabled=True
        )
        # Idempotent: the same mode appends no second event.
        assert participants.set_audio_only(
            clinic_id=w.clinic, session_id=w.session, enabled=True
        )
        assert not participants.set_audio_only(
            clinic_id=w.clinic, session_id=w.session, enabled=False
        )
    with pytest.raises(ValueError, match="invalid device result"):
        DeviceResults("ok", "ok", "ok", "Sintetico")
    with runtime_role(), patient_session_context(w.patient_session):
        participants.record_patient_device_check(
            session_id=w.session,
            results=DeviceResults("denied", "ok", "not_checked", "degraded"),
        )
        assert participants.set_patient_audio_only(session_id=w.session, enabled=True)
    assert _kinds(graph, participants_session(w)) == [
        "created",
        "joined",
        "joined",
        "audio_only",
        "video_restored",
        "audio_only",
    ]
    with runtime_role(), tenant_context(graph.physician, w.organization):
        latest = participants.latest_device_check(participants_session(w), "patient")
        assert latest is not None
        assert (latest.camera, latest.network) == ("denied", "degraded")
    assert hint_spy.count(f"teleconsult:{w.room}:teleconsult:1") == 5
    assert hint_spy.count(f"patient:{w.enrollment}:teleconsult:teleconsult:1") == 5
    assert TOPIC_PATTERN.fullmatch(f"teleconsult:{w.room}")
    assert not TOPIC_PATTERN.fullmatch(f"teleconsult:tc-{uuid4()}")


def participants_session(w: World) -> TeleconsultSession:
    with setup_context(w.organization):
        return TeleconsultRoom.objects.get(session_id=w.session).session


def test_reconnect_recovers_state_rotates_expired_credentials_and_refuses_reuse(
    rbac_graph: RbacGraph, synthetic_provider: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = seed_world(rbac_graph)
    graph = w.graph
    with runtime_role(), tenant_context(graph.physician, w.organization):
        participants.set_audio_only(
            clinic_id=w.clinic, session_id=w.session, enabled=True
        )
        recovery = participants.resume_physician(
            clinic_id=w.clinic, session_id=w.session
        )
    assert (recovery.state, recovery.audio_only, recovery.peer_present) == (
        "waiting",
        True,
        True,
    )
    assert recovery.provider_token is not None
    with setup_context(w.organization):
        old = TeleconsultCredential.objects.get(
            session_id=w.session, role="patient", revoked_at__isnull=True
        )
    real_now = timezone.now
    later = real_now() + timedelta(minutes=20)
    monkeypatch.setattr(timezone, "now", lambda: later)
    # An expired credential: its token and media actions refuse; resume
    # re-validates the patient session and rotates it.
    with runtime_role(), patient_session_context(w.patient_session):
        with pytest.raises(TeleconsultAccessDeniedError):
            enter_room(token=w.patient_token, role="patient")
        with pytest.raises(TeleconsultConflictError) as expired:
            participants.set_patient_audio_only(session_id=w.session, enabled=True)
        assert expired.value.reason_code == "not_in_room"
        rotated = participants.resume_patient(session_id=w.session)
    assert rotated.credential_expires_at > later
    monkeypatch.setattr(timezone, "now", real_now)
    with setup_context(w.organization):
        old.refresh_from_db()
        assert old.revoked_at is not None
        assert (
            TeleconsultCredential.objects.filter(
                session_id=w.session, role="patient", revoked_at__isnull=True
            ).count()
            == 1
        )
    # The expired, now rotated token is refused again after the clock returns.
    with (
        runtime_role(),
        patient_session_context(w.patient_session),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        enter_room(token=w.patient_token, role="patient")
    kinds = _kinds(graph, participants_session(w))
    assert kinds.count("reconnected") == 2
    assert kinds.count("join_denied") == 2


def test_removal_revokes_access_through_the_outbox_and_blocks_resume(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    graph = w.graph
    token = w.patient_token
    with runtime_role(), tenant_context(graph.physician, w.organization):
        participants.remove_patient(clinic_id=w.clinic, session_id=w.session)
        with pytest.raises(TeleconsultConflictError) as absent:
            participants.remove_patient(clinic_id=w.clinic, session_id=w.session)
        assert absent.value.reason_code == "not_in_room"
    with runtime_role(), patient_session_context(w.patient_session):
        with pytest.raises(TeleconsultAccessDeniedError):
            enter_room(token=token, role="patient")
        with pytest.raises(TeleconsultConflictError) as removed:
            participants.resume_patient(session_id=w.session)
        assert removed.value.reason_code == "not_in_room"
        assert participants.patient_removed(participants_session(w))
    with setup_context(w.organization):
        revoke = IntegrationOperation.objects.get(
            subject_type="teleconsult.participant"
        )
        credential = TeleconsultCredential.objects.get(pk=revoke.subject_id)
        assert (credential.role, credential.revoked_at is not None) == ("patient", True)
        assert revoke.provider == "teleconsult-synthetic-v1"
        verbs = sorted(
            AuditEvent.objects.filter(
                organization_id=w.organization, affected_record_id=str(w.session)
            ).values_list("payload__object_verb", flat=True)
        )
    assert "removed" in verbs
    with runtime_role():
        assert (
            execute_operation.apply(kwargs={"operation_id": str(revoke.pk)}).result
            == "succeeded"
        )
    with setup_context(w.organization):
        revoke.refresh_from_db()
        assert revoke.provider_reference == f"synthetic:revoke:{revoke.pk}"
    # The waiting room still admits a fresh join after removal.
    _patient_enter(
        w.patient_session, _patient_join(w.patient_session, participants_session(w))
    )
    assert _operation(graph, participants_session(w)).status == "succeeded"


def test_realtime_room_topic_admits_only_the_bound_physician(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    colleague = member(w.consent_world(), "physician", care_team=w.enrollment)
    topic = f"teleconsult:{w.room}"
    assert OPAQUE.fullmatch(w.room)
    with runtime_role(), tenant_context(w.graph.physician, w.organization):
        realtime_authorization._staff_topics((topic,))
    for actor in (colleague, w.graph.shared_user):
        with (
            runtime_role(),
            tenant_context(actor, w.organization),
            pytest.raises(realtime_authorization.TopicDeniedError),
        ):
            realtime_authorization._staff_topics((topic,))
    with (
        runtime_role(),
        tenant_context(w.graph.physician, w.organization),
        pytest.raises(realtime_authorization.TopicDeniedError),
    ):
        realtime_authorization._staff_topics(("teleconsult:tc-" + "0" * 32,))


# --------------------------------------------------------------------------
# Views that write then render: every authority first, never after a write.
# --------------------------------------------------------------------------

DEVICE_FORM = {"camera": "ok", "microphone": "ok", "speaker": "ok", "network": "good"}
VIEW_ACTIONS: dict[str, dict[str, str]] = {
    "device_check": {"action": "device_check", **DEVICE_FORM},
    "audio_only": {"action": "audio_only", "enabled": "true"},
    "remove": {"action": "remove", "surface": "workspace"},
    "resume": {"action": "resume"},
}
REPLY_MODES: dict[str, dict[str, str]] = {
    "json": {"Accept": "application/json"},
    "htmx": {"HX-Request": "true"},
    "native": {},
}
DECISIONS = ("clinic_app.has_permission(", "clinic_app.ehr_version_scope(")
WRITES = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE)\b|clinic_app\.audit_append\(", re.IGNORECASE
)


def _refused_everywhere(
    client: Client, w: World, superuser_database_url: str, label: str
) -> int:
    """Every action in every reply mode: the unknown-clinic refusal, no row."""
    before = _row_counts(superuser_database_url)
    unknown = f"/teleconsult/clinics/{uuid4()}/"
    url = f"/teleconsult/clinics/{w.clinic}/"
    decisions = 0
    for action, data in VIEW_ACTIONS.items():
        body = {"session_id": str(w.session), **data}
        for mode, headers in REPLY_MODES.items():
            reference = client.post(unknown, body, headers=headers)
            response = client.post(url, body, headers=headers)
            assert response.status_code == 403, (label, action, mode)
            assert _normalized(response.content) == _normalized(reference.content), (
                label,
                action,
                mode,
            )
            assert response.cookies.keys() == set(), (label, action, mode)
            decisions += 1
    # No record kind moved: device checks, events, credentials, outbox
    # operations, sessions, and no audit row (allow or denial).
    assert _row_counts(superuser_database_url) == before, label
    return decisions


def test_write_views_refuse_every_role_and_mode_without_any_row(
    rbac_graph: RbacGraph, synthetic_provider: object, superuser_database_url: str
) -> None:
    w = seed_world(rbac_graph)
    consent_world = w.consent_world()
    actors = {
        role: member(consent_world, role, care_team=w.enrollment) for role in ROLES
    }
    decisions = 0
    for role, actor in actors.items():
        with staff_client(actor) as client:
            assert client.get(f"/teleconsult/clinics/{uuid4()}/").status_code == 403
            decisions += _refused_everywhere(client, w, superuser_database_url, role)
    # The bound physician narrowed by a remove-only RoleGrant: same refusal.
    with setup_context(w.organization):
        RoleGrant.objects.create(
            organization_id=w.organization,
            clinic_id=w.clinic,
            role="physician",
            permission="clinical.write",
            valid_from=timezone.now() - timedelta(days=1),
        )
    with staff_client(w.graph.physician) as client:
        assert client.get(f"/teleconsult/clinics/{uuid4()}/").status_code == 403
        decisions += _refused_everywhere(client, w, superuser_database_url, "narrowed")
    assert decisions == (len(ROLES) + 1) * len(VIEW_ACTIONS) * len(REPLY_MODES)


def test_write_views_answer_every_mode_for_the_bound_physician(
    rbac_graph: RbacGraph, synthetic_provider: object, superuser_database_url: str
) -> None:
    """Positive control: each action writes and answers in each reply mode."""
    w = seed_world(rbac_graph)
    url = f"/teleconsult/clinics/{w.clinic}/"
    session = participants_session(w)
    for mode, headers in REPLY_MODES.items():
        # Re-admit the patient outside the client (its role handling is scoped).
        if mode != "json":
            _patient_enter(w.patient_session, _patient_join(w.patient_session, session))
        with staff_client(w.graph.physician) as client:
            for action, data in VIEW_ACTIONS.items():
                body = {"session_id": str(w.session), **data}
                if action == "audio_only":
                    body["enabled"] = "false" if mode == "htmx" else "true"
                before = _row_counts(superuser_database_url)
                response = client.post(url, body, headers=headers)
                assert response.status_code == 200, (mode, action)
                assert _row_counts(superuser_database_url) != before, (mode, action)


def test_native_reply_decides_every_authority_before_the_first_write(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    """Statement order: the participant permission and the clinical read are
    decided before any row or audit append of the request's view."""
    w = seed_world(rbac_graph)
    with setup_context(w.organization):
        template = SpecialtyTemplate.objects.filter(clinic_id=w.clinic).first()
        assert template is not None
        encounter = participants_session(w).encounter_id
    with runtime_role(), tenant_context(w.graph.physician, w.organization):
        create_draft(
            clinic_id=w.clinic, encounter_id=encounter, template_id=template.pk
        )
    statements: list[str] = []

    def spy(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object],
        many: bool,
        context: object,
    ) -> object:
        statements.append(sql)
        return execute(sql, params, many, context)

    with staff_client(w.graph.physician) as client:
        body = {"session_id": str(w.session), **VIEW_ACTIONS["device_check"]}
        with connection.execute_wrapper(spy):
            response = client.post(f"/teleconsult/clinics/{w.clinic}/", body)
    assert response.status_code == 200
    view_writes = [
        index
        for index, sql in enumerate(statements)
        if WRITES.search(sql) and ("teleconsult_" in sql or "audit_append" in sql)
    ]
    assert view_writes
    first_write = view_writes[0]
    for decision in DECISIONS:
        first = next(i for i, sql in enumerate(statements) if decision in sql)
        assert first < first_write, (decision, first, first_write)
