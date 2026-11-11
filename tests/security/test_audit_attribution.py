"""Repudiation rows RT-R1, SA-R2 and AG-R1 leave attributable, metadata-only records."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.ai.contracts import Purpose
from apps.ai.models import AIInvocation
from apps.ai.prompt_registry import load_prompt
from apps.identity.enterprise_access import approve_support_access, support_diagnostics
from apps.realtime.authorization import authorize_topics_sync
from apps.tenancy.db import tenant_context
from django.test import Client, override_settings
from django.utils import timezone
from ops.testing.sentinel_scan import capture_emitted_logs, new_sentinel

from ai.support import enqueue, seed
from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.lifecycle_test_support import session_key as client_session_key
from identity.permission_support import permission_context
from patient_service_support import runtime_role
from realtime.test_authorization import session_key

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

PAYLOAD_KEYS = {
    "clinic_id",
    "http_method",
    "http_status",
    "object_verb",
    "reason_code",
    "request_id",
}
FORBIDDEN = 403
REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def _events(
    superuser_database_url: str, event_type: str
) -> list[tuple[UUID | None, str | None, str | None, dict[str, object]]]:
    with psycopg.connect(superuser_database_url) as database, database.cursor() as cur:
        cur.execute(
            "SELECT actor_user_id, affected_record_type, affected_record_id, payload "
            "FROM clinic_app.audit_event WHERE event_type = %s ORDER BY seq",
            [event_type],
        )
        return list(cur.fetchall())


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_support_grant_and_read_are_audited_against_the_grant(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Given a customer admin approving a grant for a different, named support user.
    purpose = new_sentinel("SUPPORT-PURPOSE")
    setup = enterprise_setup(rbac_graph)
    approver, support_user = rbac_graph.clinic_admin, rbac_graph.physician
    support_session = client_session_key(verified_actor_client(support_user))
    with permission_context(rbac_graph, approver):
        grant = approve_support_access(
            clinic_id=rbac_graph.clinic_a,
            support_user_id=support_user,
            purpose=purpose,
            expires_at=timezone.now() + timedelta(minutes=30),
            session_key=setup.session_key,
        )
    # When that support user reads through it with their own verified session.
    with runtime_role():
        support_diagnostics(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.pk,
            session_key=support_session,
        )
    # Then the approval names the approver and the read names the support user,
    # each against the grant, and neither payload carries the purpose.
    expected = {
        "identity.support.approved": approver,
        "identity.support.read": support_user,
    }
    for event_type, actor in expected.items():
        events = _events(superuser_database_url, event_type)
        assert [row[:3] for row in events] == [
            (actor, "identity.support_access_grant", str(grant.pk))
        ]
        payload = events[0][3]
        assert set(payload) <= PAYLOAD_KEYS
        assert payload["clinic_id"] == str(rbac_graph.clinic_a)
        assert purpose not in str(payload)


def test_subscription_authorization_audits_clinic_metadata_only(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Given a staff session subscribing to its clinic agenda.
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    # When the topic is authorized.
    with runtime_role():
        authorize_topics_sync(session_key=key, topics=(topic,))
    # Then one metadata-only audit row names the subscriber and the clinic.
    events = _events(superuser_database_url, "realtime.subscription.authorized")
    assert [(actor, record) for actor, _type, record, _payload in events] == [
        (rbac_graph.shared_user, str(rbac_graph.clinic_a))
    ]
    payload = events[0][3]
    assert set(payload) <= PAYLOAD_KEYS
    assert topic not in str(payload)
    assert "agenda" not in str(payload)


def test_ticket_denial_logs_a_request_id_without_topic_contents(
    rbac_graph: RbacGraph, settings: SettingsWrapper
) -> None:
    # Given a CSRF-valid staff session asking for another clinic's agenda.
    settings.REALTIME_ENABLED = True
    client = Client(enforce_csrf_checks=True)
    client.cookies[settings.SESSION_COOKIE_NAME] = session_key(rbac_graph)
    csrf = "b" * 32
    client.cookies[settings.CSRF_COOKIE_NAME] = csrf
    topic = f"clinic:{rbac_graph.clinic_b}:agenda"
    # When the ticket endpoint refuses it.
    with capture_emitted_logs() as emitted, runtime_role():
        response = client.post(
            "/rt/stream",
            {"topics": [topic]},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        lines = [json.loads(line) for line in emitted()]
    # Then the deployment log records the registered reason and a request id,
    # never the topic or the clinic it names.
    assert response.status_code == FORBIDDEN
    denials = [
        line for line in lines if line["message"] == "realtime subscription denied"
    ]
    assert len(denials) == 1
    assert REQUEST_ID.fullmatch(denials[0]["request_id"])
    rendered = json.dumps(lines)
    assert topic not in rendered
    assert str(rbac_graph.clinic_b) not in rendered


def test_ai_invocation_records_its_provenance_outside_the_audit_payload(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    # Given an admitted front-desk invocation through the real gateway.
    world = seed(rbac_graph, monkeypatch)
    result = enqueue(world)
    assert result.invocation_id is not None
    _prompt, digest = load_prompt(Purpose.FRONT_DESK)
    # Then the AI ledger row carries purpose, configuration version, prompt
    # hash, principal and status ...
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        invocation = AIInvocation.objects.get(pk=result.invocation_id)
    assert (
        invocation.purpose,
        invocation.configuration_id,
        invocation.config_version,
        invocation.prompt_sha256,
        invocation.user_id,
        invocation.status,
    ) == (
        Purpose.FRONT_DESK,
        world.configuration.pk,
        world.configuration.version,
        digest,
        rbac_graph.shared_user,
        "reserved",
    )
    # ... and no audit payload duplicates that provenance (D-18).
    with psycopg.connect(superuser_database_url) as database:
        payloads = [
            row[0]
            for row in database.execute(
                "SELECT payload FROM clinic_app.audit_event"
            ).fetchall()
        ]
    assert payloads
    assert all(set(payload) <= PAYLOAD_KEYS for payload in payloads)
    assert digest not in json.dumps(payloads)
    assert str(world.configuration.pk) not in json.dumps(payloads)
