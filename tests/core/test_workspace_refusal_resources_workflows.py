"""Real issued-state requests cover resource and workflow refusal exits."""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows.models import WorkflowDefinitionVersion
from django.conf import settings
from django.db import connection
from django.urls import get_resolver, reverse

from core.test_navigation import _client_for, _csrf, _get, _post
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time, runtime_role
from workspace_refusal_support import workspace_routes

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.identity.models import User
    from django.http import HttpRequest, HttpResponseBase
    from django.test import Client

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def issued_manager(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> tuple[Client, User]:
    monkeypatch.setattr("apps.identity.stepup._utc_now_seconds", lambda: 2_000_000_000)
    client, user = _client_for(rbac_graph, "clinic_admin")
    with owner_context(rbac_graph.organization_a):
        assert (
            UserClinicRole.objects.filter(
                clinic_id=rbac_graph.clinic_a, user_id=user.pk
            ).update(workflow_bundle_version=2)
            == 1
        )
    return client, user


@pytest.mark.parametrize(
    "route_name",
    [
        "scheduling:resource-settings",
        "workflows:designer",
        "workflows:runs",
        "workflows:tasks",
    ],
)
def test_new_views_recheck_current_authority_after_workspace_admission(
    rbac_graph: RbacGraph,
    issued_manager: tuple[Client, User],
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
    route_name: str,
) -> None:
    client, user = issued_manager
    route = next(
        route for route in workspace_routes(get_resolver()) if route.name == route_name
    )
    url = route.url(rbac_graph.clinic_a)
    assert _get(client, url).status_code == 200
    snapshot = dict(client.session)
    original = cast("Callable[..., HttpResponseBase]", route.pattern.callback)

    @wraps(original)
    def revoke_then_enter(
        request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        # Revoke the stored authority at the exact post-admission cut point.
        # Settings keeps a live authenticated actor so its OTP decorator passes
        # and the permission loss reaches the configuration service recheck.
        with psycopg.connect(
            superuser_database_url,
            dbname=str(connection.settings_dict["NAME"]),
            autocommit=True,
        ) as owner:
            if route_name == "scheduling:resource-settings":
                changed = owner.execute(
                    "UPDATE clinic_app.identity_userclinicrole SET role='finance' "
                    "WHERE user_id=%s AND clinic_id=%s AND role='clinic_admin'",
                    [user.pk, rbac_graph.clinic_a],
                )
            else:
                changed = owner.execute(
                    "UPDATE clinic_app.identity_user SET is_active=false WHERE id=%s",
                    [user.pk],
                )
            assert changed.rowcount == 1
        return original(request, *args, **kwargs)

    monkeypatch.setattr(route.pattern, "callback", revoke_then_enter)
    response = _get(client, url)
    assert response.status_code == 403
    assert not response.cookies
    assert response.wsgi_request.session.modified is False
    assert dict(client.session) == snapshot


@pytest.mark.parametrize("route_name", ["workflows:designer", "workflows:runs"])
def test_workflow_unsupported_method_is_an_inert_in_view_refusal(
    rbac_graph: RbacGraph,
    issued_manager: tuple[Client, User],
    route_name: str,
) -> None:
    client, _user = issued_manager
    url = reverse(route_name, args=(rbac_graph.clinic_a,))
    assert _get(client, url).status_code == 200
    snapshot = dict(client.session)
    with runtime_role(), fixed_otp_time():
        response = client.delete(url, headers=_csrf(client))
    assert response.status_code == 403
    assert not response.cookies
    assert response.wsgi_request.session.modified is False
    assert dict(client.session) == snapshot


@pytest.mark.parametrize("selector", ["practitioner", "resource"])
def test_service_preparation_rejects_malformed_selectors_inside_the_view(
    rbac_graph: RbacGraph, selector: str
) -> None:
    client, _user = _client_for(rbac_graph, "receptionist")
    assert _get(client, "/auth/protected/").status_code == 200
    body = {
        "mode": "prepare",
        "enrollment_id": str(uuid4()),
        "service_type_id": str(uuid4()),
        "practitioner": "malformed" if selector == "practitioner" else "",
    }
    if selector == "resource":
        body["resource_ids"] = "malformed"
    snapshot = dict(client.session)
    response = _post(
        client,
        reverse("scheduling:appointment-create", args=(rbac_graph.clinic_a,)),
        body,
    )
    assert response.status_code == 404
    assert not response.cookies
    assert response.wsgi_request.session.modified is False
    assert dict(client.session) == snapshot


def test_workflow_unknown_commands_reach_the_valid_selector_refusal(
    rbac_graph: RbacGraph, issued_manager: tuple[Client, User]
) -> None:
    client, user = issued_manager
    designer = reverse("workflows:designer", args=(rbac_graph.clinic_a,))
    key = uuid4()
    assert (
        _post(
            client,
            designer,
            {
                "action": "create",
                "key": "synthetic-refusal-version",
                "template": "custom",
                "idempotency_key": str(key),
            },
        ).status_code
        == 302
    )
    with runtime_role(), tenant_context(user.pk, rbac_graph.organization_a):
        definition = WorkflowDefinitionVersion.objects.get(
            clinic_id=rbac_graph.clinic_a, idempotency_key=key
        )
    for url, selector in (
        (designer, {"definition_id": str(definition.pk)}),
        (
            reverse("workflows:runs", args=(rbac_graph.clinic_a,)),
            {"run_id": str(uuid4())},
        ),
    ):
        snapshot = dict(client.session)
        response = _post(
            client,
            url,
            {
                "action": "unknown",
                "expected_revision": "1",
                "idempotency_key": str(uuid4()),
                **selector,
            },
        )
        assert response.status_code == 403
        assert not response.cookies
        assert response.wsgi_request.session.modified is False
        assert dict(client.session) == snapshot


def test_workflow_invalid_creation_reaches_the_authorized_form_refusal(
    rbac_graph: RbacGraph, issued_manager: tuple[Client, User]
) -> None:
    client, _user = issued_manager
    designer = reverse("workflows:designer", args=(rbac_graph.clinic_a,))
    assert _get(client, designer).status_code == 200
    snapshot = dict(client.session)
    response = _post(client, designer, {"action": "create"})
    assert response.status_code == 400
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert response.wsgi_request.session.modified is False
    assert dict(client.session) == snapshot
