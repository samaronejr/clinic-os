"""Threat-model repudiation rows RT-R1 and SA-R2 append attributable audit rows."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.identity.enterprise_access import approve_support_access, support_diagnostics
from apps.realtime.authorization import authorize_topics_sync
from django.test import override_settings
from django.utils import timezone
from ops.testing.sentinel_scan import new_sentinel

from identity.enterprise_support import enterprise_setup
from identity.permission_support import permission_context
from patient_service_support import runtime_role
from realtime.test_authorization import session_key

if TYPE_CHECKING:
    from uuid import UUID

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


def _events(
    superuser_database_url: str, event_type: str
) -> list[tuple[UUID | None, str | None, dict[str, object]]]:
    with psycopg.connect(superuser_database_url) as database, database.cursor() as cur:
        cur.execute(
            "SELECT actor_user_id, affected_record_id, payload "
            "FROM clinic_app.audit_event WHERE event_type = %s ORDER BY seq",
            [event_type],
        )
        return list(cur.fetchall())


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_support_grant_and_read_are_audited_against_the_grant(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Given a customer-approved support grant whose purpose is a sentinel.
    purpose = new_sentinel("SUPPORT-PURPOSE")
    setup = enterprise_setup(rbac_graph)
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        grant = approve_support_access(
            clinic_id=rbac_graph.clinic_a,
            support_user_id=rbac_graph.clinic_admin,
            purpose=purpose,
            expires_at=timezone.now() + timedelta(minutes=30),
            session_key=setup.session_key,
        )
    # When the named support user reads through it.
    with runtime_role():
        support_diagnostics(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.pk,
            session_key=setup.session_key,
        )
    # Then both the approval and the read name that user and that grant only.
    for event_type in ("identity.support.approved", "identity.support.read"):
        events = _events(superuser_database_url, event_type)
        assert [(actor, record) for actor, record, _payload in events] == [
            (rbac_graph.clinic_admin, str(grant.pk))
        ]
        payload = events[0][2]
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
    assert [(actor, record) for actor, record, _payload in events] == [
        (rbac_graph.shared_user, str(rbac_graph.clinic_a))
    ]
    payload = events[0][2]
    assert set(payload) <= PAYLOAD_KEYS
    assert topic not in str(payload)
    assert "agenda" not in str(payload)
