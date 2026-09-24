from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import DataError, connection, transaction
from django.test.utils import CaptureQueriesContext

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def _runtime_role() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _context_module() -> ModuleType:
    return importlib.import_module("apps.identity.current_context")


def _add_physician(
    graph: RbacGraph,
    *,
    username: str,
    active: bool = True,
    assigned: bool = True,
) -> UUID:
    user = User.objects.create(username=username, is_active=active)
    if assigned:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(graph.organization_a)],
            )
            UserClinicRole.objects.create(
                user=user,
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                role=UserClinicRole.Role.PHYSICIAN,
            )
    return user.pk


def test_typed_catalog_returns_only_active_same_clinic_physicians_in_uuid_order(
    rbac_graph: RbacGraph,
) -> None:
    module = _context_module()
    list_physicians = getattr(module, "list_active_clinic_physicians", None)
    assert callable(list_physicians)
    active_id = _add_physician(
        rbac_graph,
        username=f"catalog-active-{uuid4().hex}",
    )
    _add_physician(
        rbac_graph,
        username=f"catalog-inactive-{uuid4().hex}",
        active=False,
    )

    with (
        _runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        CaptureQueriesContext(connection) as queries,
    ):
        catalog = list_physicians(rbac_graph.clinic_a)

    expected_ids = sorted([rbac_graph.physician, active_id])
    assert [entry.user_id for entry in catalog] == expected_ids
    assert all(
        entry.display_label == entry.display_label.casefold() for entry in catalog
    )
    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert "clinic_app.list_active_clinic_physicians" in statements
    assert 'JOIN "identity_user"' not in statements


def test_persistent_catalog_denies_nonmanager_foreign_and_malformed_context(
    rbac_graph: RbacGraph,
) -> None:
    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.physician,
            rbac_graph.organization_a,
        ),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT * FROM clinic_app.list_active_clinic_physicians(%s)",
            [str(rbac_graph.clinic_a)],
        )
        assert cursor.fetchall() == []

    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.shared_user,
            rbac_graph.organization_a,
        ),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT * FROM clinic_app.list_active_clinic_physicians(%s)",
            [str(rbac_graph.clinic_c)],
        )
        assert cursor.fetchall() == []

    with _runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            ["malformed"],
        )
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        with pytest.raises(DataError):
            cursor.execute(
                "SELECT * FROM clinic_app.list_active_clinic_physicians(%s)",
                [str(rbac_graph.clinic_a)],
            )


def test_practitioner_display_uses_resolver_current_actor_then_uuid_fallback(
    rbac_graph: RbacGraph,
) -> None:
    module = _context_module()
    display_label = getattr(module, "practitioner_display_label", None)
    assert callable(display_label)
    historical_id = _add_physician(
        rbac_graph,
        username=f"catalog-historical-{uuid4().hex}",
        assigned=False,
    )
    physician_username = User.objects.values_list("username", flat=True).get(
        pk=rbac_graph.physician
    )

    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.shared_user,
            rbac_graph.organization_a,
        ),
    ):
        active_label = display_label(rbac_graph.physician, rbac_graph.clinic_a)
        historical_label = display_label(historical_id, rbac_graph.clinic_a)
    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.physician,
            rbac_graph.organization_a,
        ),
    ):
        self_label = display_label(rbac_graph.physician, rbac_graph.clinic_a)

    assert active_label == physician_username
    assert self_label == physician_username
    assert historical_label == str(historical_id)
