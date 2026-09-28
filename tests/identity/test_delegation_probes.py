"""Root delegations decide by the named permission on real subjects."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from apps.identity.current_context import (
    CurrentActorError,
    _UnauthorizedActorError,
    require_permission,
)
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.tenancy.db import TenantAccessDeniedError
from django.db import connection

from identity.delegation_probes import CASES, Case
from identity.nonstaff_differential import root_delegation
from identity.permission_support import permission_actor, permission_context
from identity.test_permission_parity import INVENTORY
from identity.test_scope_provisioning import provisioning_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from contextlib import AbstractContextManager
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
PERMITTED_ROLE = "owner"
# Actor states outside the role catalog: no membership, and an inactive
# account that holds the permitting role.
NO_MEMBERSHIP = "none"
INACTIVE_PERMITTED = "inactive_owner"


def refused_roles(permission: str) -> list[str]:
    """Every catalog role whose versioned bundle lacks the permission."""
    return sorted(
        role
        for role in UserClinicRole.Role.values
        if permission not in BUNDLES_V1[role]
    )


def _actor(graph: RbacGraph, state: str) -> UUID:
    role = PERMITTED_ROLE if state == INACTIVE_PERMITTED else state
    actor, _ = permission_actor(graph, role)
    if state == INACTIVE_PERMITTED:
        User.objects.filter(pk=actor).update(is_active=False)
    return actor


@contextmanager
def _checked_permissions() -> Iterator[list[str]]:
    """Record every permission the application asks has_permission about."""
    asked: list[str] = []

    def spy(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if "clinic_app.has_permission(" in sql and params:
            asked.append(str(params[0]))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(spy):
        yield asked


def _permission_refusal(error: BaseException) -> bool:
    """Some exception in the chain was raised inside require_permission."""
    seen: BaseException | None = error
    while seen is not None:
        if isinstance(seen, _UnauthorizedActorError):
            traceback = seen.__traceback__
            assert traceback is not None
            while traceback.tb_next is not None:
                traceback = traceback.tb_next
            return traceback.tb_frame.f_code is require_permission.__code__
        seen = seen.__cause__ or seen.__context__
    return False


def _context(
    case: Case, graph: RbacGraph, actor: UUID
) -> AbstractContextManager[object]:
    if case.session:
        # The session names the actor; the call opens its own tenant transaction.
        return runtime_role()
    if case.database_role == "clinic_owner":
        return provisioning_context(graph, actor)
    return permission_context(graph, actor)


def test_cases_cover_every_root_delegation() -> None:
    rows = [row["symbol"] for row in INVENTORY["candidates"] if root_delegation(row)]
    assert sorted(CASES) == sorted(rows)
    for case in CASES.values():
        assert case.permission in BUNDLES_V1[PERMITTED_ROLE]
        assert refused_roles(case.permission)


@pytest.mark.parametrize("symbol", sorted(CASES))
def test_root_delegation_decides_by_the_permission(
    rbac_graph: RbacGraph, symbol: str
) -> None:
    case = CASES[symbol]
    # Every catalog role lacking the permission is refused by exactly that
    # permission check; a wrong permission name fails even where bundles overlap.
    for role in refused_roles(case.permission):
        actor = _actor(rbac_graph, role)
        seeded = case.seed(rbac_graph, actor)
        before = seeded.state()
        with (
            _checked_permissions() as asked,
            pytest.raises(seeded.denial) as refused,
            _context(case, rbac_graph, actor),
        ):
            seeded.call()
        assert _permission_refusal(refused.value), (symbol, role)
        assert asked == [case.permission], (symbol, role, asked)
        assert seeded.state() == before, (symbol, role)
    # Outside the catalog: refused and unchanged. Session checks may refuse at
    # tenant entry, which admits neither state.
    for state in (NO_MEMBERSHIP, INACTIVE_PERMITTED):
        actor = _actor(rbac_graph, state)
        seeded = case.seed(rbac_graph, actor)
        before = seeded.state()
        with (
            _checked_permissions() as asked,
            pytest.raises((seeded.denial, CurrentActorError, TenantAccessDeniedError)),
            _context(case, rbac_graph, actor),
        ):
            seeded.call()
        assert set(asked) <= {case.permission}, (symbol, state, asked)
        assert seeded.state() == before, (symbol, state)
    permitted = _actor(rbac_graph, PERMITTED_ROLE)
    seeded = case.seed(rbac_graph, permitted)
    before = seeded.state()
    with _checked_permissions() as asked, _context(case, rbac_graph, permitted):
        result = seeded.call()
    assert asked == [case.permission], (symbol, asked)
    if seeded.returns is None:
        assert seeded.state() != before, symbol
    else:
        assert result == seeded.returns(permitted), symbol
