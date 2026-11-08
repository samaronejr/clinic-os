"""Todo 50 portal reads: representation choices, channels, display and staff queue.

Every read goes through a patient-session resolver or the privacy permission;
the tests pin the exact rows a session sees, that staff contexts see nothing
through patient resolvers, and that display choices stay in the server-side
session instead of any browser storage.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.delegates import revoke, switch_delegate
from apps.identity.models import User
from apps.intake import services
from apps.intake.data_requests import submit_data_subject_request
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.intake.patient_access import PATIENT_SESSION_KEY, patient_session_context
from apps.intake.portal import DESTINATIONS, channel_preferences, delegate_options
from apps.intake.portal_display import PORTAL_DISPLAY_KEY
from apps.tenancy.db import tenant_context
from django.test import Client
from django.urls import reverse

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from otp_test_support import create_totp_device, fixed_otp_time, token_for
from patient_service_support import runtime_role
from patients.delegate_support import active_grant, delegate_world
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _patient_client(session_id: UUID) -> Client:
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(session_id)
    session.save()
    return client


def _staff_client(user_id: UUID) -> Client:
    user = User.objects.get(pk=user_id)
    device = create_totp_device(user.pk, confirmed=True)
    client = Client()
    with runtime_role():
        client.force_login(user)
        with fixed_otp_time():
            response = client.post(
                "/auth/verify/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                    "next": "/auth/protected/",
                },
            )
    assert response.status_code == 302
    return client


def test_delegate_options_list_only_active_verified_grants_of_the_original_identity(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    with runtime_role(), patient_session_context(world.session_id):
        assert delegate_options() == ()
    grant = active_grant(rbac_graph, world)
    with owner_context(rbac_graph.organization_a):
        represented = PatientClinicEnrollment.objects.get(
            pk=world.notes.enrollment_id
        ).patient
        name = Patient.objects.get(pk=represented.pk).full_name
    with runtime_role(), patient_session_context(world.session_id):
        (option,) = delegate_options()
        assert (option.grant_id, option.patient_name, option.current) == (
            grant.grant_id,
            name,
            False,
        )
        switch_delegate(clinic_id=rbac_graph.clinic_a, grant_id=grant.grant_id)
    with runtime_role(), patient_session_context(world.session_id):
        assert [(row.grant_id, row.current) for row in delegate_options()] == [
            (grant.grant_id, True)
        ]
    _, secret = _issue(rbac_graph, world.notes.enrollment_id)
    own = _redeem(rbac_graph.clinic_a, secret)
    assert own is not None
    with runtime_role(), patient_session_context(own):
        assert delegate_options() == ()
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert delegate_options() == ()
    with runtime_role(), patient_session_context(world.session_id):
        switch_delegate(clinic_id=rbac_graph.clinic_a, grant_id=None)
    with permission_context(rbac_graph, world.verifier_id):
        revoke(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.grant_id,
            expected_revision=grant.revision,
        )
    with runtime_role(), patient_session_context(world.session_id):
        assert delegate_options() == ()


def test_channel_preferences_summarize_channels_without_destinations(
    rbac_graph: RbacGraph,
) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        enrollment = services.create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Paciente Sintetico Canais",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        ).enrollment.pk
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        services.save_contact_destination(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment,
            channel="email",
            destination="paciente-sintetico-canais@example.invalid",
            expected_version=None,
        )
        services.verify_contact(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment,
            channel="email",
            expected_version=1,
        )
        services.set_purpose_channel(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment,
            purpose="appointment_reminder",
            channel="email",
        )
    _, secret = _issue(rbac_graph, enrollment)
    session = _redeem(rbac_graph.clinic_a, secret)
    assert session is not None
    with runtime_role(), patient_session_context(session):
        rows = channel_preferences()
    assert [(row.purpose, row.channel, row.opted_in, row.verified) for row in rows] == [
        ("appointment_reminder", "email", True, True)
    ]
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        assert channel_preferences() == ()
    client = _patient_client(session)
    with runtime_role():
        page = client.get("/patient/preferences/")
    assert page.status_code == 200
    assert b'data-channel-preference="appointment_reminder:email"' in page.content
    assert b"paciente-sintetico-canais" not in page.content


def test_display_choices_stay_in_the_server_session_and_refusals_change_nothing(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    client = _patient_client(world.session_id)
    with runtime_role():
        page = client.get("/patient/preferences/")
        saved = client.post(
            "/patient/preferences/",
            {"action": "display", "text_size": "large", "contrast": "high"},
        )
        home = client.get("/patient/")
        invalid = client.post(
            "/patient/preferences/",
            {"action": "display", "text_size": "huge", "contrast": "high"},
        )
        refusals = [
            client.post("/patient/preferences/", body)
            for body in ({"action": "erase"}, {"action": "push", "enable": "1"})
        ]
    assert page.status_code == 200
    assert b"data-push-unavailable" in page.content
    assert b"clinic-os-portal-large-text.css" not in page.content
    assert saved.status_code == 200
    assert client.session[PORTAL_DISPLAY_KEY] == {
        "text_size": "large",
        "contrast": "high",
    }
    assert b"clinic-os-portal-large-text.css" in home.content
    assert b"clinic-os-portal-contrast.css" in home.content
    assert invalid.status_code == 400
    assert client.session[PORTAL_DISPLAY_KEY]["text_size"] == "large"
    assert [response.status_code for response in refusals] == [403, 403]
    assert refusals[0].content == refusals[1].content
    assert all("Set-Cookie" not in response.headers for response in refusals)


def test_rollback_flag_serves_the_retained_v1_home_and_destinations(
    rbac_graph: RbacGraph, tmp_path: Path, settings: SettingsWrapper
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    _, secret = _issue(rbac_graph, world.notes.enrollment_id)
    session = _redeem(rbac_graph.clinic_a, secret)
    assert session is not None
    client = _patient_client(session)
    # The destinations that existed before portal v2 keep their v1 bytes.
    paths = (
        "/patient/",
        *(
            reverse(destination.url_name)
            for destination in DESTINATIONS
            if destination.key
            not in {"results", "messages", "preferences", "data-requests"}
        ),
    )
    with runtime_role():
        current = [client.get(path) for path in paths]
    settings.PATIENT_PORTAL_V2 = False
    with runtime_role():
        rollback = [client.get(path) for path in paths]
    assert [response.status_code for response in current + rollback] == [200] * (
        2 * len(paths)
    )
    assert b"data-portal-home" in current[0].content
    assert all(b"data-portal-back" in response.content for response in current[1:])
    assert rollback[0].templates[0].name == "intake/patient_home_v1.html"
    assert b"data-portal-home" not in rollback[0].content
    assert b'id="patient-name"' in rollback[0].content
    for response in rollback:
        assert b"data-portal-back" not in response.content
        assert b"data-portal-help" not in response.content
        assert b"clinic-os-portal.css" not in response.content


def test_home_offers_the_switcher_only_for_verified_active_grants(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    client = _patient_client(world.session_id)
    with runtime_role():
        before = client.get("/patient/")
    assert before.status_code == 200
    assert b"data-portal-switch" not in before.content
    grant = active_grant(rbac_graph, world)
    with runtime_role():
        after = client.get("/patient/")
        switched = client.post(
            "/patient/delegation/switch/",
            {"clinic_id": str(rbac_graph.clinic_a), "grant_id": str(grant.grant_id)},
        )
        acting = client.get("/patient/")
    assert b"data-portal-switch" in after.content
    assert f'value="{grant.grant_id}"'.encode() in after.content
    assert switched.status_code == 302
    assert b"data-portal-delegate" in acting.content
    assert b"selected data-portal-switch-option" in acting.content


def test_staff_queue_admits_privacy_staff_and_hides_requests_from_others(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    with runtime_role(), patient_session_context(world.session_id):
        receipt = submit_data_subject_request(
            clinic_id=rbac_graph.clinic_a,
            kind="correction",
            details="Sintetico: telefone antigo no cadastro",
            purpose="",
            idempotency_key=uuid4(),
        )
    manager, _ = permission_actor(rbac_graph, "clinic_manager")
    url = f"/intake/clinics/{rbac_graph.clinic_a}/data-requests/"
    allowed = _staff_client(manager)
    with runtime_role():
        page = allowed.get(url)
        started = allowed.post(
            url,
            {
                "action": "start",
                "request_id": str(receipt.request_id),
                "expected_revision": "1",
            },
        )
        stale = allowed.post(
            url,
            {
                "action": "start",
                "request_id": str(receipt.request_id),
                "expected_revision": "1",
            },
        )
    assert page.status_code == 200
    assert b'data-staff-request-state="received"' in page.content
    assert b"telefone antigo" in page.content
    assert started.status_code == 302
    assert stale.status_code == 409
    for role in ("receptionist", "physician", "finance"):
        actor, _ = permission_actor(rbac_graph, role)
        refused = _staff_client(actor)
        with runtime_role():
            responses = [
                refused.get(url),
                refused.post(
                    url,
                    {
                        "action": "fulfill",
                        "request_id": str(receipt.request_id),
                        "expected_revision": "2",
                    },
                ),
            ]
        assert [response.status_code for response in responses] == [404, 404], role
        assert all(b"telefone antigo" not in response.content for response in responses)


def test_staff_queue_refuses_unknown_actions_and_unlisted_reasons_in_place(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    with runtime_role(), patient_session_context(world.session_id):
        receipt = submit_data_subject_request(
            clinic_id=rbac_graph.clinic_a,
            kind="correction",
            details="Sintetico: nome social desatualizado",
            purpose="",
            idempotency_key=uuid4(),
        )
    manager, _ = permission_actor(rbac_graph, "clinic_manager")
    url = f"/intake/clinics/{rbac_graph.clinic_a}/data-requests/"
    client = _staff_client(manager)
    body = {"request_id": str(receipt.request_id), "expected_revision": "1"}
    with runtime_role():
        unknown = client.post(url, {**body, "action": "erase"})
        selectors = [
            client.post(url, {**body, "action": "start", "request_id": value})
            for value in (str(uuid4()), "not-a-request")
        ]
        unlisted = client.post(url, {**body, "action": "reject", "reason": "other"})
        page = client.get(url)
    assert [response.status_code for response in (unknown, *selectors)] == [404] * 3
    assert len({response.content for response in (unknown, *selectors)}) == 1
    assert unlisted.status_code == 400
    assert b"data-focus-error" in unlisted.content
    assert b'data-staff-request-state="received"' in page.content
