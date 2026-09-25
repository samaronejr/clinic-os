"""Every pre-v2 current-actor guard retains its four-role truth table."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import pytest
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.identity.permissions import IsClinicAdminForClinic, IsPhysicianForClinic
from apps.identity.services import ClinicId, UserId, has_clinic_role

from identity.permission_support import permission_actor, permission_context

if TYPE_CHECKING:
    from apps.identity.current_context import ClinicRoles

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[2]
LEGACY = ("owner", "physician", "receptionist", "clinic_admin")


class Guard(TypedDict):
    path: str
    function: str
    roles: str
    legacy_allowed: list[str]


GUARDS = cast(
    "list[Guard]",
    json.loads(Path(__file__).with_name("legacy_guards.json").read_text()),
)


def _roles(guard: Guard) -> ClinicRoles:
    expression = ast.parse(guard["roles"], mode="eval").body
    module = importlib.import_module(guard["path"][:-3].replace("/", "."))
    if isinstance(expression, ast.Name):
        return cast("ClinicRoles", getattr(module, expression.id))
    if isinstance(expression, ast.Tuple):
        assert all(isinstance(item, ast.Attribute) for item in expression.elts)
        return tuple(
            UserClinicRole.Role[cast("ast.Attribute", item).attr]
            for item in expression.elts
        )
    assert guard["roles"] == "tuple(UserClinicRole.Role)"
    return tuple(UserClinicRole.Role)


def test_inventory_enumerates_every_current_actor_guard() -> None:
    observed: list[tuple[str, str, str]] = []
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        for function in ast.walk(ast.parse(path.read_text())):
            if not isinstance(function, ast.FunctionDef):
                continue
            observed.extend(
                (str(path.relative_to(ROOT)), function.name, ast.unparse(node.args[1]))
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "require_current_actor_clinic_roles"
            )
    assert observed == [(g["path"], g["function"], g["roles"]) for g in GUARDS]
    assert len(observed) == 47


@pytest.mark.parametrize("legacy_role", LEGACY)
def test_all_47_guards_keep_legacy_role_decisions(
    rbac_graph: RbacGraph, legacy_role: str
) -> None:
    actor, _ = permission_actor(rbac_graph, legacy_role)
    with permission_context(rbac_graph, actor):
        for guard in GUARDS:
            roles = _roles(guard)
            assert [role for role in roles if role in LEGACY] == guard["legacy_allowed"]
            if legacy_role in guard["legacy_allowed"]:
                assert (
                    require_current_actor_clinic_roles(rbac_graph.clinic_a, roles)
                    == actor
                )
            else:
                with pytest.raises(CurrentActorError):
                    require_current_actor_clinic_roles(rbac_graph.clinic_a, roles)
            with pytest.raises(CurrentActorError):
                require_current_actor_clinic_roles(rbac_graph.clinic_b, roles)
        for permission, allowed in (
            (IsPhysicianForClinic, {"physician"}),
            (IsClinicAdminForClinic, {"owner", "clinic_admin"}),
        ):
            assert has_clinic_role(
                UserId(actor), ClinicId(rbac_graph.clinic_a), permission.required_roles
            ) == (legacy_role in allowed)
