from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.tenancy.middleware import TenantStreamingResponseError
from apps.tenancy.models import TenantProbe
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.db import connection
from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from django.urls.resolvers import URLPattern

    from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)

BACKEND_PATH = "apps.identity.auth_backends.ClinicBackend"


class ViewInterruptedError(RuntimeError):
    pass


@contextmanager
def _runtime_role() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
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


def tenant_view(request: HttpRequest) -> JsonResponse:
    user_id = request.user.pk
    count = TenantProbe.objects.count()
    labels = list(TenantProbe.objects.values_list("label", flat=True))
    user_guc, tenant_guc = _gucs()
    return JsonResponse(
        {
            "user": str(user_id),
            "count": count,
            "labels": labels,
            "user_guc": user_guc,
            "tenant_guc": tenant_guc,
        }
    )


def interrupted_view(_request: HttpRequest) -> HttpResponse:
    TenantProbe.objects.update(label="must-roll-back")
    raise ViewInterruptedError


def streaming_view(_request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(iter([b"not-supported"]))


def bypass_view(_request: HttpRequest) -> HttpResponse:
    return HttpResponse(status=204)


urlpatterns: list[URLPattern] = [
    path("tenant/", tenant_view),
    path("interrupted/", interrupted_view),
    path("stream/", streaming_view),
    path("healthz", bypass_view),
    path("readyz", bypass_view),
    path("login/", bypass_view),
    path("auth/login/", bypass_view),
    path("auth/callback/", bypass_view),
    path("static/app.css", bypass_view),
]


def test_settings_register_tenant_boundary_in_the_exact_order() -> None:
    middleware = settings.MIDDLEWARE
    auth_index = middleware.index(
        "django.contrib.auth.middleware.AuthenticationMiddleware"
    )
    otp_index = middleware.index("django_otp.middleware.OTPMiddleware")

    assert otp_index == auth_index + 1
    assert middleware[otp_index + 1] == "apps.tenancy.middleware.TenantMiddleware"
    assert settings.AUTHENTICATION_BACKENDS == [BACKEND_PATH]
    assert settings.DATABASES["default"]["ATOMIC_REQUESTS"] is False


def test_tenant_context_scopes_gucs_and_multiple_orm_operations(
    tenant_graph: TenantGraph,
) -> None:
    with _runtime_role(), CaptureQueriesContext(connection) as queries:
        with tenant_context(tenant_graph.user_a, tenant_graph.organization_a):
            assert connection.in_atomic_block
            assert _gucs() == (
                str(tenant_graph.user_a),
                str(tenant_graph.organization_a),
            )
            assert TenantProbe.objects.count() == 1
            assert list(TenantProbe.objects.values_list("label", flat=True)) == [
                "probe-a"
            ]
        _assert_gucs_empty()

    statements = [query["sql"] for query in queries.captured_queries]
    assert statements[0] == "BEGIN"
    assert "set_config('app.current_user_id'" in statements[1]
    assert "clinic_app.user_has_org" in statements[2]
    assert "set_config('app.current_tenant'" in statements[3]


def test_tenant_context_rejects_membership_without_leaking_gucs(
    tenant_graph: TenantGraph,
) -> None:
    with _runtime_role(), CaptureQueriesContext(connection) as queries:
        with (
            pytest.raises(TenantAccessDeniedError),
            tenant_context(tenant_graph.user_a, tenant_graph.organization_b),
        ):
            pytest.fail("unauthorized context yielded")
        _assert_gucs_empty()

    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert "clinic_app.user_has_org" in statements
    assert "set_config('app.current_tenant'" not in statements


@override_settings(ROOT_URLCONF=__name__)
def test_middleware_keeps_lazy_auth_and_orm_inside_tenant_transaction(
    tenant_graph: TenantGraph,
) -> None:
    client = Client()
    _seed_session(client, tenant_graph, tenant_graph.organization_a)

    with _runtime_role(), CaptureQueriesContext(connection) as queries:
        response = client.get("/tenant/")
        _assert_gucs_empty()

    assert response.status_code == 200
    assert response.json() == {
        "user": str(tenant_graph.user_a),
        "count": 1,
        "labels": ["probe-a"],
        "user_guc": str(tenant_graph.user_a),
        "tenant_guc": str(tenant_graph.organization_a),
    }
    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert "clinic_app.load_current_user()" in statements
    assert statements.index("set_config('app.current_tenant'") < statements.index(
        "clinic_app.load_current_user()"
    )


@pytest.mark.parametrize(
    ("user_value", "org_value"),
    [
        (None, None),
        ("malformed", "malformed"),
        (7, 11),
        ("user", None),
        (None, "tenant"),
    ],
)
@override_settings(ROOT_URLCONF=__name__)
def test_middleware_fails_closed_for_missing_or_malformed_session_context(
    user_value: str | int | None,
    org_value: str | int | None,
) -> None:
    client = Client()
    session = client.session
    if user_value is not None:
        session[SESSION_KEY] = user_value
    if org_value is not None:
        session["active_org_id"] = org_value
    session.save()

    response = client.get("/tenant/")

    assert response.status_code == 403
    assert response.content == b""


@override_settings(ROOT_URLCONF=__name__)
def test_middleware_rejects_unauthorized_org_and_clears_pooled_connection(
    tenant_graph: TenantGraph,
) -> None:
    client = Client()
    _seed_session(client, tenant_graph, tenant_graph.organization_b)

    with _runtime_role():
        _assert_gucs_empty()
        response = client.get("/tenant/")
        _assert_gucs_empty()

    assert response.status_code == 403
    assert response.content == b""


@pytest.mark.parametrize(
    "path", ["/healthz", "/readyz", "/auth/login/", "/static/app.css"]
)
@override_settings(ROOT_URLCONF=__name__)
def test_middleware_bypasses_only_non_tenant_paths(path: str) -> None:
    response = Client().get(path)

    assert response.status_code == 204


@pytest.mark.parametrize("path", ["/login/", "/auth/callback/"])
@override_settings(ROOT_URLCONF=__name__)
def test_middleware_does_not_bypass_other_auth_paths(path: str) -> None:
    response = Client().get(path)

    assert response.status_code == 403


@override_settings(ROOT_URLCONF=__name__)
def test_middleware_rolls_back_interrupted_view_and_reused_connection(
    tenant_graph: TenantGraph,
) -> None:
    client = Client()
    _seed_session(client, tenant_graph, tenant_graph.organization_a)

    with _runtime_role():
        with pytest.raises(ViewInterruptedError):
            client.get("/interrupted/")
        _assert_gucs_empty()
        with tenant_context(tenant_graph.user_a, tenant_graph.organization_a):
            assert TenantProbe.objects.count() == 1
            assert TenantProbe.objects.get().label == "probe-a"


@override_settings(ROOT_URLCONF=__name__)
def test_middleware_explicitly_rejects_streaming_tenant_responses(
    tenant_graph: TenantGraph,
) -> None:
    client = Client()
    _seed_session(client, tenant_graph, tenant_graph.organization_a)

    with _runtime_role(), pytest.raises(TenantStreamingResponseError):
        client.get("/stream/")
    _assert_gucs_empty()
