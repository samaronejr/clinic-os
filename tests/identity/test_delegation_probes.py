"""Root delegations decide by the named permission on real subjects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.current_context import _UnauthorizedActorError, require_permission
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1

from identity.delegation_probes import CASES, Case
from identity.nonstaff_differential import root_delegation
from identity.permission_support import permission_actor, permission_context
from identity.test_permission_parity import INVENTORY
from identity.test_scope_provisioning import provisioning_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
PERMITTED_ROLE = "owner"
# Preferred real staff lacking the permission; the catalog supplies the rest.
PREFERRED_REFUSED = ("receptionist", "physician")


def refused_roles(permission: str) -> list[str]:
    """Up to two real staff roles whose bundles lack the permission."""
    ordered = dict.fromkeys((*PREFERRED_REFUSED, *sorted(UserClinicRole.Role.values)))
    return [role for role in ordered if permission not in BUNDLES_V1[role]][:2]


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
    for role in refused_roles(case.permission):
        actor, _ = permission_actor(rbac_graph, role)
        seeded = case.seed(rbac_graph, actor)
        before = seeded.state()
        with (
            pytest.raises(seeded.denial) as refused,
            _context(case, rbac_graph, actor),
        ):
            seeded.call()
        assert _permission_refusal(refused.value), (symbol, role)
        assert seeded.state() == before, (symbol, role)
    permitted, _ = permission_actor(rbac_graph, PERMITTED_ROLE)
    seeded = case.seed(rbac_graph, permitted)
    before = seeded.state()
    with _context(case, rbac_graph, permitted):
        result = seeded.call()
    if seeded.returns is None:
        assert seeded.state() != before, symbol
    else:
        assert result == seeded.returns(permitted), symbol
