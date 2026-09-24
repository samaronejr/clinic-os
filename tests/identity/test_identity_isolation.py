from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from apps.identity.models import User, UserClinicRole
from apps.identity.services import ClinicId, UserId, has_clinic_role
from apps.tenancy.db import tenant_context
from django.db import connection
from psycopg.errors import InvalidTextRepresentation

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

COUNT_ROLES_SQL: Final = "SELECT count(*) FROM clinic_app.identity_userclinicrole"


@contextmanager
def _runtime_role() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _set_local(
    connection_: psycopg.Connection,
    setting: str,
    value: str,
) -> None:
    connection_.execute(
        "SELECT pg_catalog.set_config(%s, %s, true)",
        (setting, value),
    )


def test_organization_a_cannot_enumerate_organization_b_roles(
    rbac_graph: RbacGraph,
) -> None:
    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.shared_user,
            rbac_graph.organization_a,
        ),
    ):
        visible_organizations = set(
            UserClinicRole.objects.filter(user_id=rbac_graph.shared_user).values_list(
                "organization_id", flat=True
            )
        )

    assert visible_organizations == {rbac_graph.organization_a}


@pytest.mark.parametrize(
    ("organization_attr", "clinic_attr", "expected_role"),
    [
        ("organization_a", "clinic_a", UserClinicRole.Role.RECEPTIONIST),
        ("organization_b", "clinic_c", UserClinicRole.Role.PHYSICIAN),
    ],
)
def test_shared_user_resolves_roles_inside_each_tenant(
    rbac_graph: RbacGraph,
    organization_attr: str,
    clinic_attr: str,
    expected_role: UserClinicRole.Role,
) -> None:
    organization_id = getattr(rbac_graph, organization_attr)
    clinic_id = getattr(rbac_graph, clinic_attr)

    with _runtime_role(), tenant_context(rbac_graph.shared_user, organization_id):
        authorized = has_clinic_role(
            UserId(rbac_graph.shared_user),
            ClinicId(clinic_id),
            (expected_role,),
        )

    assert authorized is True


@pytest.mark.parametrize(
    ("organization_attr", "expected"),
    [("organization_a", False), ("organization_b", True)],
)
def test_shared_user_computed_role_follows_active_tenant(
    rbac_graph: RbacGraph,
    organization_attr: str,
    expected: bool,
) -> None:
    shared_user = User.objects.get(pk=rbac_graph.shared_user)
    organization_id = getattr(rbac_graph, organization_attr)

    with _runtime_role(), tenant_context(rbac_graph.shared_user, organization_id):
        is_physician = shared_user.is_physician_anywhere

    assert is_physician is expected


def test_role_queries_fail_closed_without_tenant_guc(
    app_database_url: str,
    rbac_graph: RbacGraph,
) -> None:
    with psycopg.connect(app_database_url) as app_connection:
        count = app_connection.execute(COUNT_ROLES_SQL).fetchone()

    assert count == (0,)


def test_hostile_tenant_setting_aborts_role_query_closed(
    app_database_url: str,
    rbac_graph: RbacGraph,
) -> None:
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(
            app_connection,
            "app.current_tenant",
            "00000000-0000-0000-0000-000000000000' OR true --",
        )

        with pytest.raises(InvalidTextRepresentation):
            app_connection.execute(COUNT_ROLES_SQL)
        app_connection.rollback()
