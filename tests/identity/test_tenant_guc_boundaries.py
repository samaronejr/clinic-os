from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import pytest
from apps.identity.models import User
from apps.tenancy.middleware import TenantStreamingResponseError
from apps.tenancy.models import TenantProbe
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.test import Client, override_settings
from django.urls import path

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from django.urls.resolvers import URLPattern

    from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)

BACKEND_PATH: Final = "apps.identity.auth_backends.ClinicBackend"
POISONED_USER_ID: Final = "11111111-1111-4111-8111-111111111111"
POISONED_TENANT_ID: Final = "22222222-2222-4222-8222-222222222222"


class ConvertedViewError(RuntimeError):
    pass


@contextmanager
def _poisoned_runtime_connection() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, false), "
            "pg_catalog.set_config('app.current_tenant', %s, false)",
            [POISONED_USER_ID, POISONED_TENANT_ID],
        )
    try:
        assert _gucs() == (POISONED_USER_ID, POISONED_TENANT_ID)
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET app.current_user_id")
            cursor.execute("RESET app.current_tenant")
            cursor.execute("RESET ROLE")


def _gucs() -> tuple[str | None, str | None]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('app.current_user_id', true), "
            "current_setting('app.current_tenant', true)"
        )
        row = cursor.fetchone()
    assert row is not None
    user_value, tenant_value = row
    assert user_value is None or isinstance(user_value, str)
    assert tenant_value is None or isinstance(tenant_value, str)
    return user_value, tenant_value


def _assert_gucs_empty() -> None:
    assert all(value in (None, "") for value in _gucs())


def _seed_session(client: Client, graph: TenantGraph, org_id: UUID) -> None:
    user = User.objects.get(pk=graph.user_a)
    session = client.session
    session[SESSION_KEY] = str(graph.user_a)
    session[BACKEND_SESSION_KEY] = BACKEND_PATH
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session["active_org_id"] = str(org_id)
    session.save()


def authorized_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "user": str(request.user.pk),
            "probe_count": TenantProbe.objects.count(),
        }
    )


def converted_error_view(_request: HttpRequest) -> HttpResponse:
    TenantProbe.objects.update(label="must-roll-back")
    raise ConvertedViewError


def streaming_view(_request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(iter([b"not-supported"]))


def bypass_view(_request: HttpRequest) -> HttpResponse:
    return HttpResponse(status=204)


urlpatterns: list[URLPattern] = [
    path("authorized/", authorized_view),
    path("converted-error/", converted_error_view),
    path("stream/", streaming_view),
    path("healthz", bypass_view),
]


@override_settings(ROOT_URLCONF=__name__)
def test_authorized_success_clears_poisoned_session_gucs_without_closing_connection(
    tenant_graph: TenantGraph,
) -> None:
    client = Client()
    _seed_session(client, tenant_graph, tenant_graph.organization_a)

    with _poisoned_runtime_connection():
        reused_connection = connection.connection
        response = client.get("/authorized/")

        assert connection.connection is reused_connection
        _assert_gucs_empty()
    assert response.status_code == 200


@override_settings(ROOT_URLCONF=__name__)
def test_unauthorized_tenant_clears_poisoned_session_gucs(
    tenant_graph: TenantGraph,
) -> None:
    client = Client()
    _seed_session(client, tenant_graph, tenant_graph.organization_b)

    with _poisoned_runtime_connection():
        response = client.get("/authorized/")

        assert response.status_code == 403
        _assert_gucs_empty()


@override_settings(ROOT_URLCONF=__name__)
def test_malformed_session_clears_poisoned_session_gucs() -> None:
    client = Client()
    session = client.session
    session[SESSION_KEY] = "malformed"
    session["active_org_id"] = "malformed"
    session.save()

    with _poisoned_runtime_connection():
        response = client.get("/authorized/")

        assert response.status_code == 403
        _assert_gucs_empty()


@override_settings(ROOT_URLCONF=__name__)
def test_missing_session_clears_poisoned_session_gucs() -> None:
    with _poisoned_runtime_connection():
        response = Client().get("/authorized/")

        assert response.status_code == 403
        _assert_gucs_empty()


@override_settings(ROOT_URLCONF=__name__)
def test_converted_view_exception_clears_poisoned_session_gucs(
    tenant_graph: TenantGraph,
) -> None:
    client = Client(raise_request_exception=False)
    _seed_session(client, tenant_graph, tenant_graph.organization_a)

    with _poisoned_runtime_connection():
        response = client.get("/converted-error/")

        assert response.status_code == 500
        _assert_gucs_empty()


@override_settings(ROOT_URLCONF=__name__)
def test_streaming_rejection_clears_poisoned_session_gucs(
    tenant_graph: TenantGraph,
) -> None:
    client = Client()
    _seed_session(client, tenant_graph, tenant_graph.organization_a)

    with _poisoned_runtime_connection():
        with pytest.raises(TenantStreamingResponseError):
            client.get("/stream/")
        _assert_gucs_empty()


@override_settings(ROOT_URLCONF=__name__)
def test_health_bypass_clears_an_open_poisoned_connection() -> None:
    with _poisoned_runtime_connection():
        reused_connection = connection.connection
        response = Client().get("/healthz")

        assert response.status_code == 204
        assert connection.connection is reused_connection
        _assert_gucs_empty()
