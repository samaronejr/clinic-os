"""Single-use, expiring, session-bound tickets at the real Redis boundary."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.realtime.authorization import TopicDeniedError
from apps.realtime.tickets import consume_ticket, issue_ticket
from apps.realtime.transport import redis_client
from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import connection, transaction
from django.test import Client
from redis import Redis

from patient_service_support import runtime_role
from realtime.test_authorization import session_key

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.usefixtures("real_redis")


def test_ticket_is_consumed_once_under_concurrent_redemption() -> None:
    topic = f"clinic:{uuid4()}:agenda"
    session = uuid4().hex
    ticket = issue_ticket(session_key=session, topics=(topic,))
    barrier = Barrier(2, timeout=5)

    def redeem() -> tuple[str, ...] | None:
        barrier.wait()
        try:
            return consume_ticket(ticket=ticket, session_key=session)
        except TopicDeniedError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: redeem(), range(2)))
    assert results.count((topic,)) == 1
    assert results.count(None) == 1
    assert topic not in ticket


def test_ticket_binds_session_and_expiry_without_a_sleep() -> None:
    topic = f"clinic:{uuid4()}:agenda"
    session = uuid4().hex
    ticket = issue_ticket(session_key=session, topics=(topic,))
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket=ticket, session_key=uuid4().hex)
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket=ticket, session_key=session)
    ticket = issue_ticket(session_key=session, topics=(topic,))
    # Observe Redis's actual TTL, then expire the exact key server-side. No
    # mocked GETDEL and no timing-luck wait for a sixty-second expiry.
    key = "rt-ticket:" + hashlib.sha256(ticket.encode()).hexdigest()
    with redis_client() as client:
        assert 0 < client.ttl(key) <= 60
        client.expire(key, 0)
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket=ticket, session_key=session)


@pytest.mark.django_db(transaction=True)
def test_ticket_http_denials_csrf_and_private_headers(rbac_graph: RbacGraph) -> None:
    key = session_key(rbac_graph)
    client = Client(enforce_csrf_checks=True)
    client.cookies[settings.SESSION_COOKIE_NAME] = key
    path = "/rt/stream"
    payload = {"topics": [f"clinic:{rbac_graph.clinic_a}:agenda"]}
    with runtime_role():
        assert (
            client.post(path, payload, content_type="application/json").status_code
            == 403
        )
        csrf = "a" * 32
        client.cookies[settings.CSRF_COOKIE_NAME] = csrf
        response = client.post(
            path, payload, content_type="application/json", HTTP_X_CSRFTOKEN=csrf
        )
    assert response.status_code == 200
    assert set(response.json()) == {"ticket"}
    assert "no-store" in response["Cache-Control"]
    assert "script-src 'self'" in response["Content-Security-Policy"]
    bodies = []
    for topic in (
        f"clinic:{rbac_graph.clinic_b}:agenda",
        f"clinic:{uuid4()}:agenda",
        "SINTETICO-SENTINELA-PHI",
    ):
        with runtime_role():
            denied = client.post(
                path,
                {"topics": [topic]},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf,
            )
        assert denied.status_code == 403
        bodies.append(denied.content)
    assert len(set(bodies)) == 1


@pytest.fixture
def isolated_keyspace(
    real_redis: None, redis_url: str, settings: SettingsWrapper
) -> Iterator[Redis]:
    """Point the app at a private, flushed database index of the owned Redis.

    The index is the instance's last configured database, never the shared
    index the session's other realtime tests (and their expiring tickets)
    use, so an exact whole-keyspace comparison sees only this test's writes.
    """
    shared = urlsplit(redis_url)
    with Redis.from_url(redis_url) as admin:
        databases = int(admin.config_get("databases")["databases"])
    index = databases - 1
    assert index != int(shared.path.lstrip("/") or "0")
    private = urlunsplit(shared._replace(path=f"/{index}"))
    settings.REALTIME_REDIS_URL = private
    with Redis.from_url(private) as client:
        client.flushdb()
        yield client
        client.flushdb()


def keyspace(client: Redis) -> dict[bytes, bytes | None]:
    """Every key in the private index with its serialized value."""
    return {name: client.dump(name) for name in client.scan_iter()}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["inactive", "revoked"])
def test_inactive_or_revoked_actor_cannot_obtain_a_ticket(
    rbac_graph: RbacGraph, state: str, isolated_keyspace: Redis, redis_url: str
) -> None:
    key = session_key(rbac_graph)
    csrf = "a" * 32
    client = Client(enforce_csrf_checks=True)
    client.cookies[settings.SESSION_COOKIE_NAME] = key
    client.cookies[settings.CSRF_COOKIE_NAME] = csrf

    def request(clinic: object) -> tuple[int, bytes, set[str]]:
        with runtime_role():
            response = client.post(
                "/rt/stream",
                {"topics": [f"clinic:{clinic}:agenda"]},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf,
            )
        return response.status_code, response.content, set(response.cookies)

    granted = request(rbac_graph.clinic_a)
    assert granted[0] == 200
    # The app under test really writes to the private index, not the shared one.
    name = (
        "rt-ticket:"
        + hashlib.sha256(json.loads(granted[1])["ticket"].encode()).hexdigest()
    )
    assert isolated_keyspace.exists(name) == 1
    with Redis.from_url(redis_url) as shared:
        assert shared.exists(name) == 0
    unknown = request(uuid4())
    assert unknown[0] == 403
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        if state == "inactive":
            user = User.objects.get(pk=rbac_graph.shared_user)
            user.is_active = False
            user.save(update_fields=("is_active",))
        else:
            UserClinicRole.objects.filter(
                user_id=rbac_graph.shared_user, clinic_id=rbac_graph.clinic_a
            ).delete()
    stored = Session.objects.values_list("session_data", "expire_date").get(pk=key)
    # Start the refusal from an empty private keyspace: no earlier key (and no
    # TTL) can move under the exact comparison below.
    isolated_keyspace.flushdb()
    before = keyspace(isolated_keyspace)
    assert before == {}
    # Byte-identical to the unknown-topic refusal; no Redis key of any name,
    # cookie or session write accompanies the refusal (SC-1).
    assert request(rbac_graph.clinic_a) == unknown
    assert keyspace(isolated_keyspace) == before
    assert (
        Session.objects.values_list("session_data", "expire_date").get(pk=key) == stored
    )


@pytest.mark.parametrize("ticket", ["", "../", "x" * 1000, "SINTETICO-SENTINELA-PHI"])
def test_forged_ticket_is_refused(ticket: str) -> None:
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket=ticket, session_key=uuid4().hex)
