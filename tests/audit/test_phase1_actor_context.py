from __future__ import annotations

import importlib
import importlib.util
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
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


def _current_context() -> ModuleType:
    module_name = "apps.identity.current_context"
    assert importlib.util.find_spec(module_name) is not None
    return importlib.import_module(module_name)


def _context_error(module: ModuleType) -> type[Exception]:
    error_type = getattr(module, "CurrentActorError", None)
    assert isinstance(error_type, type)
    assert issubclass(error_type, Exception)
    return error_type


def test_current_actor_id_comes_only_from_the_guc_bound_resolver(
    rbac_graph: RbacGraph,
) -> None:
    module = _current_context()
    current_actor_id = getattr(module, "current_actor_id", None)
    assert callable(current_actor_id)

    with (
        _runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        CaptureQueriesContext(connection) as queries,
    ):
        actor_id = current_actor_id()

    assert actor_id == rbac_graph.physician
    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert "clinic_app.load_current_user()" in statements
    assert 'FROM "identity_user"' not in statements


def test_current_actor_rejects_missing_and_malformed_guc() -> None:
    module = _current_context()
    current_actor_id = getattr(module, "current_actor_id", None)
    assert callable(current_actor_id)
    error_type = _context_error(module)

    with _runtime_role(), pytest.raises(error_type, match="current actor unavailable"):
        current_actor_id()

    with _runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            ["not-a-uuid"],
        )
        with pytest.raises(error_type, match="current actor unavailable"):
            current_actor_id()


def test_current_actor_rejects_nonexistent_and_inactive_users(
    rbac_graph: RbacGraph,
) -> None:
    module = _current_context()
    current_actor_id = getattr(module, "current_actor_id", None)
    assert callable(current_actor_id)
    error_type = _context_error(module)

    with _runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(UUID(int=0))],
        )
        with pytest.raises(error_type, match="current actor unavailable"):
            current_actor_id()

    User.objects.filter(pk=rbac_graph.physician).update(is_active=False)
    with (
        _runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        pytest.raises(error_type, match="current actor unavailable"),
    ):
        current_actor_id()


def test_required_clinic_roles_derive_the_actor_and_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    module = _current_context()
    require_roles = getattr(module, "require_current_actor_clinic_roles", None)
    assert callable(require_roles)
    error_type = _context_error(module)

    with (
        _runtime_role(),
        tenant_context(
            rbac_graph.physician,
            rbac_graph.organization_a,
        ),
    ):
        actor_id = require_roles(
            rbac_graph.clinic_a,
            (UserClinicRole.Role.PHYSICIAN,),
        )
        with pytest.raises(error_type, match="current actor unauthorized"):
            require_roles(
                rbac_graph.clinic_b,
                (UserClinicRole.Role.PHYSICIAN,),
            )

    assert actor_id == rbac_graph.physician
