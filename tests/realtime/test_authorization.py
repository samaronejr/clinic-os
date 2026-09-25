"""Todo 8: real-role subscription authorization, never frontend authority."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import RoleGrant, User, UserClinicRole
from apps.realtime.authorization import TopicDeniedError, authorize_topics_sync
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_static.models import StaticDevice

from otp_test_support import create_totp_device, current_user_guc
from patient_service_support import runtime_role
from renewal.test_self_booking import _client, _session
from scheduling.appointment_service_support import seed_appointment_setup

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def session_key(graph: RbacGraph) -> str:
    client = Client()
    session = client.session
    user = User.objects.get(pk=graph.shared_user)
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "apps.identity.auth_backends.ClinicBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session["active_org_id"] = str(graph.organization_a)
    session.save()
    assert session.session_key is not None
    return session.session_key


def test_topics_recheck_permission_and_close_database(rbac_graph: RbacGraph) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    with runtime_role():
        grants = authorize_topics_sync(session_key=key, topics=(topic,))
        assert grants.topics == (topic,)
        assert grants.user_id == rbac_graph.shared_user
        assert not connection.in_atomic_block
        assert connection.connection is None


@pytest.mark.parametrize("kind", ["foreign", "unknown", "malformed", "inbox", "ai"])
def test_unknown_and_forbidden_topics_have_identical_denial(
    rbac_graph: RbacGraph, kind: str
) -> None:
    topics = {
        "foreign": f"clinic:{rbac_graph.clinic_b}:agenda",
        "unknown": f"clinic:{uuid4()}:agenda",
        "malformed": "SINTETICO-SENTINELA-TOPIC",
        "inbox": f"clinic:{rbac_graph.clinic_a}:inbox",
        "ai": f"ai_job:{uuid4().hex}",
    }
    key = session_key(rbac_graph)
    with runtime_role():
        with pytest.raises(TopicDeniedError, match="subscription unavailable"):
            authorize_topics_sync(session_key=key, topics=(topics[kind],))
        assert connection.connection is None


def test_narrowed_role_cannot_mint_or_reauthorize(rbac_graph: RbacGraph) -> None:
    key = session_key(rbac_graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role=UserClinicRole.Role.RECEPTIONIST,
            permission="appointment.read",
            valid_from=timezone.now(),
        )
    with runtime_role():
        with pytest.raises(TopicDeniedError):
            authorize_topics_sync(
                session_key=key, topics=(f"clinic:{rbac_graph.clinic_a}:agenda",)
            )
        assert connection.connection is None


@pytest.mark.parametrize(
    "state", ["verified", "missing", "pending", "foreign", "static"]
)
def test_privileged_subscription_keeps_exact_totp_policy(
    rbac_graph: RbacGraph, state: str
) -> None:
    user = User.objects.get(pk=rbac_graph.clinic_admin)
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "apps.identity.auth_backends.ClinicBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session["active_org_id"] = str(rbac_graph.organization_a)
    if state == "static":
        with runtime_role(), current_user_guc(user.pk):
            device = StaticDevice.objects.create(user_id=user.pk, confirmed=True)
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    elif state != "missing":
        device = create_totp_device(
            rbac_graph.physician if state == "foreign" else user.pk,
            confirmed=state != "pending",
        )
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    assert session.session_key is not None
    topics = (f"clinic:{rbac_graph.clinic_b}:agenda",)
    with runtime_role():
        if state == "verified":
            assert (
                authorize_topics_sync(
                    session_key=session.session_key, topics=topics
                ).user_id
                == user.pk
            )
        else:
            with pytest.raises(TopicDeniedError):
                authorize_topics_sync(session_key=session.session_key, topics=topics)
        assert connection.connection is None


@pytest.mark.parametrize("state", ["expired", "changed_password", "malformed_org"])
def test_session_authentication_is_not_cached(
    rbac_graph: RbacGraph, state: str
) -> None:
    key = session_key(rbac_graph)
    session = SessionStore(session_key=key)
    if state == "expired":
        session.set_expiry(-1)
    elif state == "changed_password":
        session[HASH_SESSION_KEY] = uuid4().hex
    else:
        session["active_org_id"] = "SINTETICO-SENTINELA-PHI"
    session.save()
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(
            session_key=key, topics=(f"clinic:{rbac_graph.clinic_a}:agenda",)
        )


def test_patient_session_never_becomes_clinic_wide_staff_authority(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    client = _client(_session(setup))
    key = client.session.session_key
    assert key is not None
    with runtime_role(), CaptureQueriesContext(connection) as queries:
        with pytest.raises(TopicDeniedError):
            authorize_topics_sync(
                session_key=key, topics=(f"clinic:{setup.clinic_id}:agenda",)
            )
        assert connection.connection is None
    statements = [query["sql"] for query in queries.captured_queries]
    assert any("touch_patient_session" in sql for sql in statements)
    assert not any("set_config('app.current_user_id'" in sql for sql in statements)
