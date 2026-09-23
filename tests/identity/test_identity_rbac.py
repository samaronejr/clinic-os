from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final
from urllib.parse import quote
from uuid import UUID

import pytest
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import IsClinicAdminForClinic, IsPhysicianForClinic
from apps.identity.services import (
    ClinicId,
    ClinicRoleQuerySetMixin,
    UserId,
    has_clinic_role,
)
from apps.tenancy.db import tenant_context
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.urls.resolvers import URLPattern
    from rest_framework.request import Request

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

BACKEND_PATH: Final = "apps.identity.auth_backends.ClinicBackend"
RBAC_RAW_CREDENTIAL: Final = "todo8-correct-horse-battery-staple"


@contextmanager
def _runtime_role() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _seed_session(client: Client, graph: RbacGraph, user_id: UUID) -> None:
    user = User.objects.get(pk=user_id)
    session = client.session
    session[SESSION_KEY] = str(user_id)
    session[BACKEND_SESSION_KEY] = BACKEND_PATH
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session["active_org_id"] = str(graph.organization_a)
    session.save()


class PhysicianClinicView(APIView):
    permission_classes = (IsPhysicianForClinic,)

    def get(self, _request: Request, clinic_id: str) -> Response:
        return Response({"clinic_id": clinic_id})


class AdminClinicView(APIView):
    permission_classes = (IsClinicAdminForClinic,)

    def get(self, _request: Request, clinic_id: str) -> Response:
        return Response({"clinic_id": clinic_id})


class PhysicianClinicScope(ClinicRoleQuerySetMixin):
    required_roles = (UserClinicRole.Role.PHYSICIAN,)


urlpatterns: list[URLPattern] = [
    path("rbac/<str:clinic_id>/", PhysicianClinicView.as_view()),
    path("rbac-admin/<str:clinic_id>/", AdminClinicView.as_view()),
]


def test_physician_is_denied_an_unassigned_clinic(rbac_graph: RbacGraph) -> None:
    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.physician,
            rbac_graph.organization_a,
        ),
    ):
        assigned = has_clinic_role(
            UserId(rbac_graph.physician),
            ClinicId(rbac_graph.clinic_a),
            (UserClinicRole.Role.PHYSICIAN,),
        )
        unassigned = has_clinic_role(
            UserId(rbac_graph.physician),
            ClinicId(rbac_graph.clinic_b),
            (UserClinicRole.Role.PHYSICIAN,),
        )

    assert assigned is True
    assert unassigned is False


def test_role_scoped_queryset_returns_exact_granted_clinics(
    rbac_graph: RbacGraph,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            user_id=rbac_graph.physician,
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            role=UserClinicRole.Role.PHYSICIAN,
        )

    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.physician,
            rbac_graph.organization_a,
        ),
    ):
        clinic_ids = list(
            PhysicianClinicScope()
            .clinics_for_user(UserId(rbac_graph.physician))
            .values_list("pk", flat=True)
        )

    assert clinic_ids == sorted([rbac_graph.clinic_a, rbac_graph.clinic_b])


def test_computed_role_properties_query_only_memberships(
    rbac_graph: RbacGraph,
) -> None:
    physician = User.objects.get(pk=rbac_graph.physician)
    clinic_admin = User.objects.get(pk=rbac_graph.clinic_admin)

    with (
        _runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        CaptureQueriesContext(connection) as queries,
    ):
        values = (
            physician.is_physician_anywhere,
            physician.is_clinic_admin_anywhere,
            clinic_admin.is_clinic_admin_anywhere,
        )

    assert values == (True, False, True)
    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert 'FROM "identity_userclinicrole"' in statements
    assert 'FROM "identity_user"' not in statements
    assert 'JOIN "identity_user"' not in statements


@pytest.mark.parametrize(
    ("clinic_attr", "expected_status"),
    [("clinic_a", 200), ("clinic_b", 403)],
)
@override_settings(ROOT_URLCONF=__name__)
def test_drf_permission_enforces_clinic_assignment(
    rbac_graph: RbacGraph,
    clinic_attr: str,
    expected_status: int,
) -> None:
    client = Client()
    physician = User.objects.get(pk=rbac_graph.physician)
    physician.groups.add(Group.objects.create(name="physician"))
    _seed_session(client, rbac_graph, rbac_graph.physician)

    with _runtime_role():
        response = client.get(f"/rbac/{getattr(rbac_graph, clinic_attr)}/")

    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("clinic_attr", "expected_status"),
    [("clinic_b", 200), ("clinic_a", 403)],
)
@override_settings(ROOT_URLCONF=__name__)
def test_admin_permission_enforces_clinic_assignment(
    rbac_graph: RbacGraph,
    clinic_attr: str,
    expected_status: int,
) -> None:
    client = Client()
    _seed_session(client, rbac_graph, rbac_graph.clinic_admin)

    with _runtime_role():
        response = client.get(f"/rbac-admin/{getattr(rbac_graph, clinic_attr)}/")

    assert response.status_code == expected_status


@pytest.mark.parametrize(
    "hostile_clinic_id",
    ["not-a-uuid", "00000000-0000-0000-0000-000000000000'OR'1'='1"],
)
@override_settings(ROOT_URLCONF=__name__)
def test_drf_permission_fails_closed_for_hostile_clinic_identifiers(
    rbac_graph: RbacGraph,
    hostile_clinic_id: str,
) -> None:
    client = Client()
    _seed_session(client, rbac_graph, rbac_graph.physician)

    with _runtime_role():
        response = client.get(f"/rbac/{quote(hostile_clinic_id, safe='')}/")

    assert response.status_code == 403


def test_login_selects_deterministic_membership_without_user_table_access(
    rbac_graph: RbacGraph,
) -> None:
    client = Client()
    session = client.session
    session["active_org_id"] = str(UUID(int=0))
    session.save()

    with _runtime_role(), CaptureQueriesContext(connection) as queries:
        authenticated = client.login(
            username=rbac_graph.shared_username,
            password=RBAC_RAW_CREDENTIAL,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('app.current_user_id', true), "
                "current_setting('app.current_tenant', true)"
            )
            gucs = cursor.fetchone()

    assert authenticated is True
    assert client.session["active_org_id"] == str(
        min(rbac_graph.organization_a, rbac_graph.organization_b)
    )
    assert gucs in ((None, None), ("", ""))
    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert "clinic_app.auth_lookup" in statements
    assert "clinic_app.user_organizations()" in statements
    assert 'FROM "identity_user"' not in statements
    assert 'UPDATE "identity_user"' not in statements
    assert (
        User.objects.values_list("last_login", flat=True).get(pk=rbac_graph.shared_user)
        is None
    )


def test_login_without_membership_clears_stale_active_organization(
    rbac_graph: RbacGraph,
) -> None:
    client = Client()
    session = client.session
    session["active_org_id"] = str(rbac_graph.organization_a)
    session.save()

    with _runtime_role():
        authenticated = client.login(
            username=rbac_graph.no_membership_username,
            password=RBAC_RAW_CREDENTIAL,
        )

    assert authenticated is True
    assert "active_org_id" not in client.session
