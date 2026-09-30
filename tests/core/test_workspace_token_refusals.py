"""Real token issuance, followed by revocation or stale saved-view state."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core import command_tokens
from apps.core.patient_context import (
    register_context_switch_guard,
    unregister_context_switch_guard,
)
from apps.core.workspace import ACTIVE_CLINIC_SESSION_KEY
from django.conf import settings
from django.utils.translation import gettext
from django_otp import DEVICE_ID_SESSION_KEY

from core.test_navigation import (
    CLOSE,
    OPTIONS,
    PATIENT,
    RUN,
    _client_for,
    _FakeGuard,
    _get,
    _patient_token,
    _post,
)
from core.test_saved_views import _option_value, _options
from core.test_workspace_boundaries import remove_permission
from patient_http_support import seed_patients

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _unchanged(
    client: Client, response: _MonkeyPatchedWSGIResponse, before: dict[str, object]
) -> None:
    assert response.status_code == 404
    assert response.wsgi_request.session.modified is False
    assert dict(client.session) == before
    assert not response.cookies


def test_valid_patient_token_then_revoked_permission(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    seed_patients(rbac_graph, user.pk, rbac_graph.clinic_a, (PATIENT,))
    value = _patient_token(client, rbac_graph)
    assert ACTIVE_CLINIC_SESSION_KEY not in client.session
    remove_permission(rbac_graph, "receptionist", "demographics.read")
    before = dict(client.session)
    _unchanged(client, _post(client, RUN, {"token": value}), before)


def test_valid_archive_token_then_archived_subject(rbac_graph: RbacGraph) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    week = f"/scheduling/clinics/{rbac_graph.clinic_a}/agenda/week/2035-06-04/1/"
    assert _get(client, week).status_code == 200
    name = f"{gettext('Agenda')} \u00b7 {gettext('Week')}"
    save_label = gettext("Save this view: %(name)s") % {"name": name}
    remove_label = gettext("Remove saved view: %(name)s") % {"name": name}
    value = _option_value(_options(client, week), save_label)
    assert _post(client, RUN, {"token": value, "next": week}).status_code == 302
    first = _option_value(_options(client, week), remove_label)
    second = _option_value(_options(client, week), remove_label)
    assert _post(client, RUN, {"token": first, "next": week}).status_code == 302
    before = dict(client.session)
    _unchanged(client, _post(client, RUN, {"token": second, "next": week}), before)


@pytest.mark.parametrize(
    ("kind", "subject"),
    [("patient", "invalid"), ("unknown", "invalid"), ("save_view", "invalid")],
)
def test_valid_tokens_with_unusable_kind_or_subject(
    rbac_graph: RbacGraph, kind: str, subject: str
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    session = client.session
    value = command_tokens.issue(
        session, clinic_id=rbac_graph.clinic_a, kind=kind, subject=subject
    )
    session.save()
    before = dict(client.session)
    _unchanged(client, _post(client, RUN, {"token": value}), before)


def test_close_refuses_bound_work_without_selecting_default(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    guard = _FakeGuard()
    register_context_switch_guard("refusal-exits", guard)
    try:
        response = _post(client, CLOSE, {})
    finally:
        unregister_context_switch_guard("refusal-exits")
    assert response.status_code == 409
    assert response.wsgi_request.session.modified is False
    assert ACTIVE_CLINIC_SESSION_KEY not in client.session
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert not guard.discarded


def test_options_refuses_unverified_privileged_actor(rbac_graph: RbacGraph) -> None:
    client, _ = _client_for(rbac_graph, "clinic_admin")
    session = client.session
    session.pop(DEVICE_ID_SESSION_KEY)
    session.save()
    response = _post(client, OPTIONS, {"q": str(uuid4())})
    assert response.status_code == 403
    assert response.wsgi_request.session.modified is False
    assert not response.cookies
