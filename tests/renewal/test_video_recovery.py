"""Synthetic recovery contracts through the real outbox, EHR and FORCE RLS.

The callback fixture authenticates exact bytes, but is NOT a provider protocol.
Room-ended webhooks have no approved implementation: they fail closed rather
than being confused with a delivery callback or an authorized physician end.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.comms.adapters import (
    AuthenticatedCallback,
    CallbackAuthenticationError,
    SendResult,
    TransientSendError,
)
from apps.core import integration
from apps.ehr.models import ClinicalDocumentVersion, Encounter, SpecialtyTemplate
from apps.ehr.services import create_draft, record_clinical_note
from apps.intake.patient_access import patient_session_context
from apps.teleconsult.adapters import PROVIDER, SyntheticRoomAdapter
from apps.teleconsult.capabilities import room_capability
from apps.teleconsult.models import (
    TeleconsultCredential,
    TeleconsultRoom,
    TeleconsultSession,
)
from apps.teleconsult.services import (
    TeleconsultAccessDeniedError,
    TeleconsultConflictError,
    end_consultation,
    enter_room,
    refresh_session,
    request_patient_join,
    request_physician_join,
    start_consultation,
)
from apps.tenancy.db import tenant_context
from django.db import connection
from django.utils import timezone

from patient_service_support import runtime_role
from provider_gate_support import assert_capability_gate_closed
from renewal.test_encounters import CONTENT, setup_context
from renewal.test_teleconsult_sessions import (
    _create,
    _kinds,
    _operation,
    _patient_enter,
    _patient_join,
    _physician_enter,
    _physician_join,
    _run_room,
    seed,
    synthetic_provider,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from conftest import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)
KEY = b"task-30-synthetic-callback-key-not-a-provider-secret"


class SignedSyntheticCallback:
    provider = PROVIDER

    def authenticate(
        self, *, headers: Mapping[str, str], body: bytes
    ) -> AuthenticatedCallback:
        expected = hmac.new(KEY, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("signature", ""), expected):
            raise CallbackAuthenticationError
        data = json.loads(body)
        if data["status"] not in ("delivered", "failed"):
            raise CallbackAuthenticationError
        return AuthenticatedCallback(data["event"], data["reference"], data["status"])


@pytest.fixture
def callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        integration._CALLBACK_AUTHENTICATORS, PROVIDER, SignedSyntheticCallback()
    )


def callback(reference: str, status: str, event: str) -> str:
    body = json.dumps(
        {"reference": reference, "status": status, "event": event}
    ).encode()
    with runtime_role():
        return integration.receive_provider_callback(
            provider=PROVIDER,
            headers={"signature": hmac.new(KEY, body, hashlib.sha256).hexdigest()},
            body=body,
        )


def saved_note(graph: RbacGraph, encounter: Encounter) -> ClinicalDocumentVersion:
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        template = SpecialtyTemplate.objects.get(clinic_id=graph.clinic_a)
        draft = create_draft(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, template_id=template.pk
        )
        return record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=1,
            content=CONTENT,
        )


def assert_notes(graph: RbacGraph, note: ClinicalDocumentVersion) -> None:
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        stored = ClinicalDocumentVersion.objects.get(pk=note.pk)
        assert stored.revision == 2
        assert stored.state == "draft"
        assert {field: getattr(stored, field) for field in CONTENT} == CONTENT
        assert Encounter.objects.get(pk=stored.document.encounter_id).state == "open"


def test_duplicate_creation_and_end_preserve_history_and_notes(
    rbac_graph: RbacGraph, synthetic_provider: SyntheticRoomAdapter
) -> None:
    graph = rbac_graph
    _, encounter, _, patient, _ = seed(graph)
    note = saved_note(graph, encounter)
    session = _create(graph, encounter)
    assert _create(graph, encounter).pk == session.pk
    assert _run_room(graph, session) == "succeeded"
    assert _run_room(graph, session) == "skipped"
    _patient_enter(patient, _patient_join(patient, session))
    _physician_enter(graph, _physician_join(graph, session))
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        started = start_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
        assert (
            start_consultation(clinic_id=graph.clinic_a, session_id=session.pk).revision
            == started.revision
        )
        ended = end_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
        duplicate = end_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
        assert (duplicate.revision, duplicate.ended_at) == (
            ended.revision,
            ended.ended_at,
        )
        assert TeleconsultRoom.objects.filter(session=session).count() == 1
        assert not TeleconsultCredential.objects.filter(
            session=session, revoked_at__isnull=True
        ).exists()
    assert _create(graph, encounter).state == "ended"
    assert _kinds(graph, session) == ["created", "joined", "joined", "started", "ended"]
    assert_notes(graph, note)


@pytest.mark.parametrize("role", ["patient", "physician"])
def test_disconnect_reconnect_rotation_and_expiry_recheck_authority(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    graph = rbac_graph
    _, encounter, _, patient, _ = seed(graph)
    note = saved_note(graph, encounter)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    token = (
        _patient_join(patient, session)
        if role == "patient"
        else _physician_join(graph, session)
    )

    def enter(value: str, *, denied: bool = False) -> None:
        authority = (
            patient_session_context(patient)
            if role == "patient"
            else tenant_context(graph.physician, graph.organization_a)
        )
        with runtime_role(), authority:
            if denied:
                # Catch inside the request transaction, like the HTTP view,
                # so the metadata-only denial event commits with the response.
                with pytest.raises(TeleconsultAccessDeniedError):
                    enter_room(token=value, role=role)
            else:
                enter_room(token=value, role=role)

    enter(token)
    # Transport disconnect does not mutate domain state; replaying the same
    # admission is idempotent and the exact joined event is not duplicated.
    enter(token)
    assert _kinds(graph, session) == ["created", "joined"]
    with setup_context(graph.organization_a):
        credential = TeleconsultCredential.objects.get(session=session, role=role)
    now = timezone.now
    with monkeypatch.context() as clock:
        clock.setattr(
            timezone, "now", lambda: credential.expires_at + timedelta(seconds=1)
        )
        enter(token, denied=True)
    assert timezone.now is now
    renewed = (
        _patient_join(patient, session)
        if role == "patient"
        else _physician_join(graph, session)
    )
    enter(token, denied=True)
    enter(renewed)
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        request_physician_join(clinic_id=graph.clinic_a, session_id=session.pk)
    # Expiry moves the service clock forward; compare event multiplicity,
    # not wall-clock ordering after restoring that clock.
    assert sorted(_kinds(graph, session)) == [
        "created",
        "join_denied",
        "join_denied",
        "joined",
        "joined",
    ]
    assert_notes(graph, note)


class LostRoomResponse(SyntheticRoomAdapter):
    """The remote effect succeeds but its first response is lost in transit."""

    def __init__(self) -> None:
        self.effects: dict[UUID, str] = {}
        self.calls = 0

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        assert not connection.in_atomic_block
        result = super().send(prepared, operation_id=operation_id)
        self.effects.setdefault(operation_id, result.provider_reference)
        self.calls += 1
        if self.calls == 1:
            raise TransientSendError from TimeoutError("synthetic transport timeout")
        return result


def test_timeout_after_remote_creation_retries_the_same_room(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    _, encounter, _, _, _ = seed(graph)
    session = _create(graph, encounter)
    adapter = LostRoomResponse()
    monkeypatch.setitem(integration._SEND_ADAPTERS, PROVIDER, adapter)
    assert _run_room(graph, session) == "retry"
    assert _operation(graph, session).provider_reference is None
    assert _run_room(graph, session) == "succeeded"
    assert _run_room(graph, session) == "skipped"
    assert adapter.calls == 2
    assert adapter.effects == {
        _operation(graph, session).pk: f"synthetic:room:tc-{session.pk}"
    }
    assert _kinds(graph, session) == ["created"]


def test_provider_outage_exhaustion_is_terminal_but_notes_survive(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    settings: SettingsWrapper,
) -> None:
    graph = rbac_graph
    _, encounter, _, patient, _ = seed(graph)
    note = saved_note(graph, encounter)
    session = _create(graph, encounter)
    settings.TELECONSULT_SYNTHETIC_FAIL = True
    assert [_run_room(graph, session) for _ in range(4)] == [
        "retry",
        "retry",
        "retry",
        "failed",
    ]
    settings.TELECONSULT_SYNTHETIC_FAIL = False
    assert _run_room(graph, session) == "skipped"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        failed = refresh_session(session)
        assert (failed.state, failed.failure_reason) == ("failed", "room_unavailable")
        assert refresh_session(failed).revision == failed.revision
        with pytest.raises(TeleconsultConflictError):
            request_physician_join(clinic_id=graph.clinic_a, session_id=session.pk)
    with (
        runtime_role(),
        patient_session_context(patient),
        pytest.raises(TeleconsultConflictError),
    ):
        request_patient_join(session_id=session.pk)
    assert _kinds(graph, session) == ["created", "failed"]
    assert_notes(graph, note)


@pytest.mark.parametrize("first", ["delivered", "failed"])
def test_duplicate_and_out_of_order_authenticated_room_callbacks(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    callbacks: None,
    first: str,
) -> None:
    graph = rbac_graph
    _, encounter, _, patient, _ = seed(graph)
    note = saved_note(graph, encounter)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    token = _patient_join(patient, session)
    reference = f"synthetic:room:tc-{session.pk}"
    assert callback(reference, first, "one") == "applied"
    assert callback(reference, first, "one") == "duplicate"
    assert (
        callback(reference, "failed" if first == "delivered" else "delivered", "late")
        == "duplicate"
    )
    assert _operation(graph, session).status == first
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        current = refresh_session(session)
        assert current.state == ("failed" if first == "failed" else "waiting")
    if first == "failed":
        # Failure closes admission by session state even when the retained
        # credential row itself has not been explicitly revoked.
        with pytest.raises(TeleconsultConflictError):
            _patient_enter(patient, token)
    else:
        _patient_enter(patient, token)
    assert_notes(graph, note)


def test_late_callbacks_cannot_reopen_an_ended_consultation(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    callbacks: None,
) -> None:
    graph = rbac_graph
    _, encounter, _, patient, _ = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    token = _patient_join(patient, session)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        ended = end_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
    assert callback(f"synthetic:room:tc-{session.pk}", "delivered", "late") == "applied"
    with pytest.raises(TeleconsultConflictError):
        _patient_enter(patient, token)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        current = TeleconsultSession.objects.get(pk=session.pk)
        assert (current.state, current.revision, current.ended_at) == (
            "ended",
            ended.revision,
            ended.ended_at,
        )
        with pytest.raises(TeleconsultConflictError):
            start_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
    assert _kinds(graph, session) == ["created", "ended"]


def test_unapproved_end_callbacks_and_forged_callbacks_fail_closed(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    callbacks: None,
    settings: SettingsWrapper,
) -> None:
    graph = rbac_graph
    _, encounter, _, _, _ = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    for _ in range(2):
        with pytest.raises(CallbackAuthenticationError):
            callback(f"synthetic:room:tc-{session.pk}", "ended", "end")
    with runtime_role(), pytest.raises(CallbackAuthenticationError):
        integration.receive_provider_callback(
            provider=PROVIDER, headers={}, body=b"forged"
        )
    assert _operation(graph, session).status == "succeeded"
    assert _kinds(graph, session) == ["created"]
    assert room_capability().synthetic_enabled
    assert_capability_gate_closed(settings, "video", probe=room_capability)
