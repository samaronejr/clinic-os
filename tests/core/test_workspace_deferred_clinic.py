"""A fresh session resolves clinic context without committing a later refusal."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core import command_tokens
from apps.core.patient_context import (
    register_context_switch_guard,
    unregister_context_switch_guard,
)
from apps.core.workspace import (
    ACTIVE_CLINIC_SESSION_KEY,
    PENDING_CLINIC_ATTRIBUTE,
    current_clinic,
)
from apps.identity.models import UserClinicRole
from apps.tenancy.middleware import TenantMiddleware
from django.http import Http404, HttpRequest, HttpResponse
from django.test import RequestFactory
from django.urls import resolve

from core.test_navigation import RUN, _client_for, _get, _post
from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from apps.core.patient_context import ContextSwitch, SwitchConsequence

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class _RefusedSwitch:
    def check(self, switch: ContextSwitch) -> SwitchConsequence | None:
        raise Http404

    def discard(self, switch: ContextSwitch) -> None:
        pytest.fail("A refused switch must not discard work")


@pytest.mark.parametrize("multi_clinic", [False, True])
@pytest.mark.parametrize("remembered", ["fresh", "foreign", "malformed"])
@pytest.mark.parametrize("action", ["run", "patient", "save_view", "close"])
def test_fresh_post_refusals_do_not_persist_clinic(
    rbac_graph: RbacGraph, multi_clinic: bool, remembered: str, action: str
) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "receptionist")
    if multi_clinic:
        with owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                user_id=user.pk,
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_b,
                role="receptionist",
            )
    # No page, including /auth/protected/, has been loaded on this session.
    session = client.session
    assert ACTIVE_CLINIC_SESSION_KEY not in session
    if remembered != "fresh":
        session[ACTIVE_CLINIC_SESSION_KEY] = (
            str(graph.clinic_c) if remembered == "foreign" else "invalid"
        )
    token = str(uuid4())
    if action in {"patient", "save_view"}:
        # Valid server-issued token; its subject cannot be accepted by the view.
        token = command_tokens.issue(
            session,
            clinic_id=graph.clinic_a,
            kind=action,
            subject=str(uuid4()),
        )
    session.save()
    before = dict(client.session)
    register_context_switch_guard("refused-selection", _RefusedSwitch())
    try:
        response = _post(
            client,
            "/workspace/patient/close/" if action == "close" else RUN,
            {"token": token},
        )
    finally:
        unregister_context_switch_guard("refused-selection")
    assert response.status_code == 404
    assert response.wsgi_request.session.modified is False
    assert not response.cookies
    assert dict(client.session) == before
    authorized = _get(client, f"/scheduling/clinics/{graph.clinic_a}/agenda/")
    assert authorized.status_code == 200
    assert client.session[ACTIVE_CLINIC_SESSION_KEY] == str(graph.clinic_a)


@pytest.mark.parametrize("status", [200, 302, 400, 401, 403, 404, 405, 409, 500, None])
def test_every_response_and_exception_finishes_pending_selection(
    rbac_graph: RbacGraph, status: int | None
) -> None:
    client, user = _client_for(rbac_graph, "receptionist")
    request = RequestFactory().get("/workspace/command/")
    request.session = client.session
    request.user = user
    request.resolver_match = resolve(request.path_info)
    before = dict(request.session)

    def view(incoming: HttpRequest) -> HttpResponse:
        selected = current_clinic(incoming)
        assert selected is not None
        assert selected.id == rbac_graph.clinic_a
        assert incoming.session.modified is False
        assert ACTIVE_CLINIC_SESSION_KEY not in incoming.session
        if status is None:
            raise RuntimeError
        return HttpResponse(status=status)

    with runtime_role():
        if status is None:
            with pytest.raises(RuntimeError):
                TenantMiddleware(view)(request)
        else:
            response = TenantMiddleware(view)(request)
            assert response.status_code == status
    assert not hasattr(request, PENDING_CLINIC_ATTRIBUTE)
    if status is not None and status < 400:
        assert request.session[ACTIVE_CLINIC_SESSION_KEY] == str(rbac_graph.clinic_a)
        assert request.session.modified is True
    else:
        assert dict(request.session) == before
        assert request.session.modified is False
