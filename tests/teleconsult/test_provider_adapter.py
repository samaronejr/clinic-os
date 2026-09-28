"""The VideoProvider boundary, its recorded fixtures and the room binding trigger.

LiveKit and Twilio adapters run only against recorded synthetic exchanges
(``fixtures/*.json``); the default transport refuses without opening a socket
and live adapters are BLOCKED-ON-EG. The database half proves the rewritten
``teleconsult_binding_guard``: a room binds only to a ``video`` capability
version whose provider equals its outbox operation's provider, room names are
opaque, and the recording/transcription refusal is still the trigger's own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.ehr.services import open_encounter
from apps.intake.models import PatientClinicEnrollment
from apps.providers.models import CapabilityVersion
from apps.scheduling.services import AppointmentLocalRange, create_appointment
from apps.teleconsult import services, video_providers
from apps.teleconsult.migrations._teleconsult_v2_sql import (
    BINDING_GUARD_V1,
    CAPTURE_REFUSAL,
)
from apps.teleconsult.models import (
    TeleconsultEvent,
    TeleconsultRoom,
    TeleconsultSession,
)
from apps.teleconsult.video_providers import (
    LIVE_PROVIDERS,
    LIVEKIT_PROVIDER,
    SYNTHETIC_PROVIDER,
    TWILIO_PROVIDER,
    BlockedTransport,
    LiveKitVideoProvider,
    ProviderBlockedError,
    ProviderChoice,
    ProviderRequest,
    ProviderRequestError,
    ProviderResponse,
    ProviderUnavailableError,
    RevokeSpec,
    RoomSpec,
    SyntheticVideoProvider,
    TokenGrant,
    TokenInvalidError,
    TwilioVideoProvider,
    VideoProvider,
    select_room_provider,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from patient_service_support import runtime_role
from provider_gate_support import activate_capability
from renewal.test_encounters import setup_context
from renewal.test_teleconsult_sessions import (
    _create,
    seed,
    synthetic_provider,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOM = "tc-0123456789abcdef0123456789abcdef"
ROOM_BINDING = "invalid teleconsult room binding"
PROVIDER_BINDING = "invalid teleconsult provider binding"


def _fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / f"{name}.json").read_text())
    assert isinstance(value, dict)
    return value


def _clock(recording: dict[str, object]) -> datetime:
    return datetime.fromisoformat(str(recording["clock"]))


def _b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def decode_jwt(
    token: str, secret: bytes
) -> tuple[dict[str, object], dict[str, object]]:
    """Verify an HS256 signature independently of the adapter, then decode."""
    header, claims, signature = token.split(".")
    expected = hmac.new(
        secret, f"{header}.{claims}".encode("ascii"), hashlib.sha256
    ).digest()
    assert hmac.compare_digest(_b64decode(signature), expected)
    return json.loads(_b64decode(header)), json.loads(_b64decode(claims))


class RecordedTransport:
    """Replay recorded exchanges in order; every request must match exactly."""

    def __init__(self, exchanges: list[dict[str, object]], secret: bytes) -> None:
        self.pending = list(exchanges)
        self.secret = secret
        self.sent: list[ProviderRequest] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        recorded = self.pending.pop(0)
        expected = recorded["request"]
        assert isinstance(expected, dict)
        headers = dict(request.headers)
        assert request.method == expected["method"]
        assert request.url == expected["url"]
        authorization = expected.get("authorization")
        if isinstance(authorization, dict):
            scheme, token = headers.pop("Authorization").split(" ", 1)
            assert scheme == authorization["scheme"]
            header, claims = decode_jwt(token, self.secret)
            assert header == authorization["jwt_header"]
            assert claims == authorization["claims"]
        assert headers == expected["headers"]
        if "json" in expected:
            assert json.loads(request.body) == expected["json"]
        else:
            assert dict(parse_qsl(request.body.decode("ascii"))) == expected["form"]
            assert len(parse_qsl(request.body.decode("ascii"))) == len(expected["form"])
        self.sent.append(request)
        response = recorded["response"]
        assert isinstance(response, dict)
        return ProviderResponse(
            int(response["status"]), json.dumps(response["json"]).encode()
        )


def _exchanges(recording: dict[str, object], *names: str) -> list[dict[str, object]]:
    exchanges = recording["exchanges"]
    assert isinstance(exchanges, list)
    by_name = {item["operation"]: item for item in exchanges}
    return [by_name[name] for name in names]


def livekit(*names: str) -> tuple[LiveKitVideoProvider, RecordedTransport]:
    recording = _fixture("livekit")
    credentials = recording["credentials"]
    assert isinstance(credentials, dict)
    secret = str(credentials["api_secret"]).encode()
    transport = RecordedTransport(_exchanges(recording, *names), secret)
    adapter = LiveKitVideoProvider(
        host=str(credentials["host"]),
        api_key=str(credentials["api_key"]),
        api_secret=secret,
        transport=transport,
        clock=lambda: _clock(recording),
    )
    return adapter, transport


def twilio(*names: str) -> tuple[TwilioVideoProvider, RecordedTransport]:
    recording = _fixture("twilio")
    credentials = recording["credentials"]
    assert isinstance(credentials, dict)
    transport = RecordedTransport(
        _exchanges(recording, *names), str(credentials["api_key_secret"]).encode()
    )
    adapter = TwilioVideoProvider(
        account_sid=str(credentials["account_sid"]),
        api_key_sid=str(credentials["api_key_sid"]),
        api_key_secret=str(credentials["api_key_secret"]),
        transport=transport,
        clock=lambda: _clock(recording),
    )
    return adapter, transport


def test_every_adapter_implements_the_video_provider_protocol() -> None:
    adapters: list[VideoProvider] = [
        SyntheticVideoProvider(),
        livekit()[0],
        twilio()[0],
    ]
    assert [adapter.key for adapter in adapters] == [
        SYNTHETIC_PROVIDER,
        LIVEKIT_PROVIDER,
        TWILIO_PROVIDER,
    ]
    # The keys are the seeded registry's version providers, not free text.
    assert {LIVEKIT_PROVIDER, TWILIO_PROVIDER} == LIVE_PROVIDERS
    for adapter in adapters:
        for method in ("create_room", "mint_token", "revoke"):
            assert callable(getattr(adapter, method))


def test_livekit_recorded_create_revoke_and_denied_response() -> None:
    adapter, transport = livekit("create_room", "revoke", "create_room_denied")
    receipt = adapter.create_room(RoomSpec(ROOM))
    assert (receipt.provider, receipt.reference) == (
        LIVEKIT_PROVIDER,
        "livekit:room:RM_sintetico01",
    )
    revoked = adapter.revoke(RevokeSpec(ROOM, "patient"))
    assert revoked.reference == f"livekit:revoke:{ROOM}:patient"
    with pytest.raises(ProviderRequestError):
        adapter.create_room(RoomSpec(ROOM))
    assert transport.pending == []
    assert len(transport.sent) == 3


def test_twilio_recorded_create_never_records_and_revoke_disconnects() -> None:
    adapter, transport = twilio("create_room", "revoke", "create_room_denied")
    receipt = adapter.create_room(RoomSpec(ROOM))
    assert (receipt.provider, receipt.reference) == (
        TWILIO_PROVIDER,
        "twilio:room:RM0123456789abcdef0123456789abcdef",
    )
    body = dict(parse_qsl(transport.sent[0].body.decode()))
    assert body["RecordParticipantsOnConnect"] == "false"
    assert adapter.revoke(RevokeSpec(ROOM, "patient")).reference == (
        f"twilio:revoke:{ROOM}:patient"
    )
    with pytest.raises(ProviderRequestError):
        adapter.create_room(RoomSpec(ROOM))
    assert transport.pending == []


@pytest.mark.parametrize("name", ["livekit", "twilio"])
def test_minted_tokens_match_the_recording_and_live_fifteen_minutes(
    name: str,
) -> None:
    recording = _fixture(name)
    token = recording["token"]
    credentials = recording["credentials"]
    assert isinstance(token, dict)
    assert isinstance(credentials, dict)
    adapter: VideoProvider = livekit()[0] if name == "livekit" else twilio()[0]
    secret = str(
        credentials["api_secret" if name == "livekit" else "api_key_secret"]
    ).encode()
    minted = adapter.mint_token(
        TokenGrant(ROOM, str(token["identity"]), _clock(recording))
    )
    header, claims = decode_jwt(minted.token, secret)
    assert header == token["jwt_header"]
    assert claims == token["claims"]
    assert isinstance(claims["exp"], int)
    assert isinstance(claims["nbf"], int)
    assert claims["exp"] - claims["nbf"] == 900
    assert minted.expires_at == _clock(recording) + timedelta(minutes=15)
    # The only identity anywhere in the token is the role; no UUID appears.
    decoded = json.dumps(claims)
    assert not any(
        len(part) == 36 and part.count("-") == 4
        for part in decoded.replace('"', " ").split()
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: RoomSpec("tc-" + str(uuid4())),
        lambda: RoomSpec(f"tc-{uuid4()}".replace("-", "")[:35]),
        lambda: RoomSpec("sala-do-paciente-sintetico"),
        lambda: RevokeSpec(ROOM, "Sintetico Paciente"),
        lambda: TokenGrant(ROOM, "patient", datetime.now(UTC), timedelta(minutes=16)),
        lambda: TokenGrant(ROOM, "patient", datetime.now(UTC), timedelta(0)),
        lambda: TokenGrant(ROOM, "patient", datetime(2026, 9, 28, 12, 0)),  # noqa: DTZ001 - naive on purpose
        lambda: TokenGrant(ROOM, "admin", datetime.now(UTC)),
    ],
)
def test_boundary_refuses_identifiers_long_tokens_and_foreign_identities(
    build: Callable[[], object],
) -> None:
    with pytest.raises(ProviderRequestError):
        build()


def test_blocked_transport_and_live_adapters_never_reach_a_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        attempts.append((args, kwargs))
        message = "network access attempted"
        raise AssertionError(message)

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    recording = _fixture("livekit")
    credentials = recording["credentials"]
    assert isinstance(credentials, dict)
    adapter = LiveKitVideoProvider(
        host=str(credentials["host"]),
        api_key=str(credentials["api_key"]),
        api_secret=str(credentials["api_secret"]).encode(),
        transport=BlockedTransport(),
        clock=lambda: _clock(recording),
    )
    with pytest.raises(ProviderBlockedError):
        adapter.create_room(RoomSpec(ROOM))
    with pytest.raises(ProviderBlockedError):
        adapter.revoke(RevokeSpec(ROOM, "patient"))
    for provider in (*sorted(LIVE_PROVIDERS), SYNTHETIC_PROVIDER, "Daily", ""):
        with pytest.raises(ProviderBlockedError):
            video_providers.live_provider(provider)
    assert attempts == []


def test_synthetic_tokens_expire_bind_one_room_and_refuse_tampering() -> None:
    provider = SyntheticVideoProvider()
    issued = datetime.now(UTC)
    minted = provider.mint_token(TokenGrant(ROOM, "physician", issued))
    assert provider.verify_token(minted.token, room_name=ROOM, now=issued) == (
        "physician"
    )
    # Expired token reuse is denied at the provider boundary too.
    with pytest.raises(TokenInvalidError):
        provider.verify_token(
            minted.token, room_name=ROOM, now=issued + timedelta(minutes=15)
        )
    with pytest.raises(TokenInvalidError):
        provider.verify_token(minted.token, room_name="tc-" + "f" * 32, now=issued)
    with pytest.raises(TokenInvalidError):
        provider.verify_token(minted.token[:-2] + "xx", room_name=ROOM, now=issued)
    assert provider.create_room(RoomSpec(ROOM)).reference == f"synthetic:room:{ROOM}"


# --------------------------------------------------------------------------
# Selection and the database binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomWorld:
    graph: RbacGraph
    session: TeleconsultSession
    bare: TeleconsultSession


@pytest.fixture
def room_world(rbac_graph: RbacGraph, synthetic_provider: object) -> RoomWorld:
    """One provisioned session, plus a second session stored without a room."""
    graph = rbac_graph
    appointment, encounter, acceptance, _, _ = seed(graph)
    session = _create(graph, encounter)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        )
        second = create_appointment(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment.pk,
            practitioner_id=graph.physician,
            local_range=AppointmentLocalRange("2035-06-02T10:30", "2035-06-02T11:00"),
            idempotency_key=uuid4(),
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        other = open_encounter(clinic_id=graph.clinic_a, appointment_id=second.pk)
    with setup_context(graph.organization_a):
        bare = TeleconsultSession.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            encounter_id=other.pk,
            appointment_id=second.pk,
            patient_id=appointment.patient_id,
            physician_id=graph.physician,
            consent_id=acceptance.pk,
        )
    return RoomWorld(graph, session, bare)


def forge(  # noqa: PLR0913 - one forged row per varied binding input
    w: RoomWorld,
    *,
    provider: str = SYNTHETIC_PROVIDER,
    environment: str = "synthetic",
    operation_provider: str | None = None,
    room_name: str | None = None,
    recording: bool = False,
    transcription: bool = False,
    runtime: bool = False,
) -> str | None:
    """Insert one room for the bare session; return the refusal text or None."""
    graph = w.graph
    with setup_context(graph.organization_a):
        operation = IntegrationOperation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            actor_id=graph.physician,
            channel="video",
            provider=operation_provider or provider,
            subject_type="teleconsult.session",
            subject_id=w.bare.pk,
            idempotency_key=uuid4(),
        )
    fields = {
        "organization_id": graph.organization_a,
        "operation_id": operation.pk,
        "session_id": w.bare.pk,
        "room_name": room_name or f"tc-{secrets.token_hex(16)}",
        "provider": provider,
        "provider_environment": environment,
        "recording_enabled": recording,
        "transcription_enabled": transcription,
    }
    context = (
        tenant_context(graph.physician, graph.organization_a)
        if runtime
        else setup_context(graph.organization_a)
    )
    with runtime_role() if runtime else transaction.atomic(), context:
        try:
            with transaction.atomic():
                TeleconsultRoom.objects.create(**fields)
        except DatabaseError as error:
            return str(error)
        transaction.set_rollback(True)
    return None


pytestmark_db = pytest.mark.django_db(transaction=True)


@pytestmark_db
def test_rooms_bind_only_to_a_video_version_equal_to_the_operation_provider(
    room_world: RoomWorld,
) -> None:
    w = room_world
    with setup_context(w.graph.organization_a):
        room = TeleconsultRoom.objects.get(session_id=w.session.pk)
        operation = IntegrationOperation.objects.get(pk=room.operation_id)
    # The stored room names the synthetic version and its operation's provider.
    assert (room.provider, room.provider_environment) == (
        SYNTHETIC_PROVIDER,
        "synthetic",
    )
    assert operation.provider == room.provider
    assert forge(w) is None
    # The operation's provider differs from the room's version: refused.
    assert ROOM_BINDING in str(forge(w, operation_provider=LIVEKIT_PROVIDER))
    # A provider/environment pair that is no video version: refused.
    assert PROVIDER_BINDING in str(forge(w, provider="Sintetico Provider"))
    assert PROVIDER_BINDING in str(forge(w, environment="production"))
    # A registered but unapproved real version cannot bind a room.
    assert PROVIDER_BINDING in str(
        forge(w, provider=LIVEKIT_PROVIDER, environment="production")
    )
    # Empty identity is only for rooms stored before v2.
    assert PROVIDER_BINDING in str(forge(w, provider="", environment=""))
    # The runtime role (the assigned physician's own write) is refused alike.
    assert ROOM_BINDING in str(
        forge(w, operation_provider=TWILIO_PROVIDER, runtime=True)
    )


@pytestmark_db
def test_activated_real_version_binds_by_equality_not_by_a_hardcoded_key(
    room_world: RoomWorld,
) -> None:
    w = room_world
    activate_capability("video")
    version = CapabilityVersion.objects.get(
        capability__key="video",
        capability__clinic_id__isnull=True,
        provider=LIVEKIT_PROVIDER,
    )
    assert version.state == "activated"
    assert forge(w, provider=LIVEKIT_PROVIDER, environment="production") is None
    assert ROOM_BINDING in str(
        forge(
            w,
            provider=LIVEKIT_PROVIDER,
            environment="production",
            operation_provider=SYNTHETIC_PROVIDER,
        )
    )
    # Activation of the real version never makes synthetic rooms real, and the
    # synthetic version still binds only as itself.
    assert ROOM_BINDING in str(forge(w, operation_provider=LIVEKIT_PROVIDER))


@pytestmark_db
@pytest.mark.parametrize(
    ("flags", "label"),
    [
        ({"recording": True}, "recording"),
        ({"transcription": True}, "transcription"),
        ({"recording": True, "transcription": True}, "both"),
    ],
)
def test_capture_flags_are_refused_by_the_unchanged_trigger_line(
    room_world: RoomWorld, flags: dict[str, bool], label: str
) -> None:
    refusal = forge(
        room_world,
        recording=flags.get("recording", False),
        transcription=flags.get("transcription", False),
    )
    # The trigger fires before the CHECK constraint; its own refusal proves
    # the :117 line, not just the model constraint, still rejects capture.
    assert refusal is not None, label
    assert ROOM_BINDING in refusal, label
    assert "teleconsult_room_capture_disabled" not in refusal, label
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_get_functiondef("
            "'clinic_app.teleconsult_binding_guard()'::regprocedure)"
        )
        row = cursor.fetchone()
    assert row is not None
    assert (
        CAPTURE_REFUSAL + "\n     RAISE EXCEPTION 'invalid teleconsult room " in row[0]
    )
    assert CAPTURE_REFUSAL + "\n" in BINDING_GUARD_V1


@pytestmark_db
def test_room_names_can_never_spell_a_stored_identifier(room_world: RoomWorld) -> None:
    w = room_world
    bare = w.bare
    for identifier in (
        bare.pk,
        bare.encounter_id,
        bare.appointment_id,
        bare.patient_id,
        bare.physician_id,
        bare.clinic_id,
        bare.organization_id,
    ):
        assert ROOM_BINDING in str(forge(w, room_name=f"tc-{identifier.hex}"))
    for legacy in (f"tc-{bare.pk}", "tc-" + "0" * 31, "sala-sintetica"):
        assert ROOM_BINDING in str(forge(w, room_name=legacy))


@pytestmark_db
def test_selection_never_falls_back_and_refusal_writes_nothing(
    room_world: RoomWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = room_world
    graph = w.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        choice = select_room_provider(clinic_id=graph.clinic_a)
    assert choice == ProviderChoice(SYNTHETIC_PROVIDER, "synthetic")
    # A live registry answer must be served by the live version's adapter,
    # which is blocked: refused even though the synthetic gate is on.
    monkeypatch.setattr(video_providers, "is_live", lambda *_a, **_k: True)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ProviderUnavailableError),
    ):
        select_room_provider(clinic_id=graph.clinic_a)
    monkeypatch.undo()
    settings.TELECONSULT_SYNTHETIC_PROVIDER = False
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ProviderUnavailableError),
    ):
        select_room_provider(clinic_id=graph.clinic_a)
    # create_session refuses with the fixed conflict and writes no row.
    encounter = _open_second_encounter(w)
    with setup_context(graph.organization_a):
        before = (
            TeleconsultSession.objects.count(),
            TeleconsultRoom.objects.count(),
            IntegrationOperation.objects.count(),
            TeleconsultEvent.objects.count(),
            AuditEvent.objects.filter(organization_id=graph.organization_a).count(),
        )
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(services.TeleconsultConflictError) as refused,
    ):
        services.create_session(clinic_id=graph.clinic_a, encounter_id=encounter)
    assert refused.value.reason_code == "provider_unavailable"
    with setup_context(graph.organization_a):
        after = (
            TeleconsultSession.objects.count(),
            TeleconsultRoom.objects.count(),
            IntegrationOperation.objects.count(),
            TeleconsultEvent.objects.count(),
            AuditEvent.objects.filter(organization_id=graph.organization_a).count(),
        )
    assert after == before


def _open_second_encounter(w: RoomWorld) -> UUID:
    graph = w.graph
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=w.session.patient_id
        )
        appointment = create_appointment(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment.pk,
            practitioner_id=graph.physician,
            local_range=AppointmentLocalRange("2035-06-02T11:00", "2035-06-02T11:30"),
            idempotency_key=uuid4(),
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        return open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        ).pk
