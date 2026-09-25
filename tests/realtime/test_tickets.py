"""Single-use, expiring, session-bound tickets at the real Redis boundary."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.realtime.authorization import TopicDeniedError
from apps.realtime.tickets import consume_ticket, issue_ticket
from apps.realtime.transport import redis_client
from django.conf import settings
from django.test import Client

from patient_service_support import runtime_role
from realtime.test_authorization import session_key

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.usefixtures("real_redis")


def test_ticket_is_consumed_once_under_concurrent_redemption() -> None:
    topic = f"clinic:{uuid4()}:agenda"
    session = uuid4().hex
    ticket = issue_ticket(session, (topic,))
    barrier = Barrier(2, timeout=5)

    def redeem() -> tuple[str, ...] | None:
        barrier.wait()
        try:
            return consume_ticket(ticket, session)
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
    ticket = issue_ticket(session, (topic,))
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket, uuid4().hex)
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket, session)
    ticket = issue_ticket(session, (topic,))
    # Observe Redis's actual TTL, then expire the exact key server-side. No
    # mocked GETDEL and no timing-luck wait for a sixty-second expiry.
    key = "rt-ticket:" + hashlib.sha256(ticket.encode()).hexdigest()
    with redis_client() as client:
        assert 0 < client.ttl(key) <= 60
        client.expire(key, 0)
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket, session)


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


@pytest.mark.parametrize("ticket", ["", "../", "x" * 1000, "SINTETICO-SENTINELA-PHI"])
def test_forged_ticket_is_refused(ticket: str) -> None:
    with pytest.raises(TopicDeniedError):
        consume_ticket(ticket, uuid4().hex)
