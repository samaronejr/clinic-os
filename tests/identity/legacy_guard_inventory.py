"""Broad authorization census; every candidate must be reviewed and classified.

The detector deliberately over-collects. The manifest records direct executable
probes, delegated guards, non-staff principals, and non-authorization data work.
It never silently discards an unfamiliar scope/access function or SQL resolver.
"""

from __future__ import annotations

import ast
import re
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING

from identity.legacy_sql_inventory import discover_sql
from identity.staff_state_analysis import _imports, _name

__all__ = ["declared_probes", "discover", "discover_role_calls", "discover_sql"]

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[2]
SIGNALS = {
    "role_helper": (
        r"\b(?:require_current_actor_(?:clinic_roles|org_admin)|has_clinic_role|"
        r"clinics_for_user_roles)\("
    ),
    "membership": (
        r"UserClinicRole\.objects|userclinicrole__|"
        r"is_physician_anywhere|is_clinic_admin_anywhere"
    ),
    "actor": r"\b(?:current_actor_id|current_actor_username|_load_current_actor)\(",
    "resolver": r"clinic_app\.[a-z_]+\(",
    "denial": (
        r"raise\s+(?:\w*AccessDenied\w*|PermissionDenied|CurrentActorError)"
        r"|\b_denied\("
    ),
    "scope_name": (
        r"^def (?:authorized_\w+|authorize_\w+|\w*_scope|\w*_access|"
        r"\w*_authority|_is_manager|_is_physician|_assigned|_denied|"
        r"has_permission|require_\w+)\("
    ),
    "authentication": (
        r"is_authenticated|is_privileged_user|is_confirmed_verified_user|"
        r"assert_step_up\(|_freshness_is_valid\(|assert_owner_database_role\("
    ),
    "decorator": (
        r"@(?:privileged_totp_required|login_required|require_recent_verification)"
    ),
    "tenant_boundary": r"\b(?:tenant_context|patient_session_context)\(",
    "operations_auth": (
        r"OPS_METRICS_(?:TOKEN|NETWORKS)_ENV|"
        r"\b(?:_allowed_networks|_client_ip_allowed|_ops_token_valid)\("
    ),
}


def _functions(
    nodes: list[ast.stmt], prefix: str = ""
) -> Iterator[tuple[str, ast.FunctionDef]]:
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            yield from _functions(node.body, prefix + node.name + ".")
        elif isinstance(node, ast.FunctionDef):
            # Nested wrappers are exercised by their actual public factory;
            # do not count the same nested body twice as two public APIs.
            yield prefix + node.name, node


def discover() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            # Python entry points only. discover_sql independently inventories
            # SQL definitions here and deployed policy/resolver dependencies.
            continue
        module = str(path.relative_to(ROOT))[:-3].replace("/", ".")
        tree = ast.parse(path.read_text())
        aliases = _imports(tree, module)
        for name, node in _functions(tree.body):
            body = ast.unparse(node)
            signals = sorted(
                key
                for key, pattern in SIGNALS.items()
                if re.search(pattern, body, re.MULTILINE)
            )
            if any(
                isinstance(call, ast.Call)
                and _name(call.func, aliases).rsplit(".", 1)[-1]
                in {"require_permission", "has_permission", "granted_permissions"}
                for call in ast.walk(node)
            ):
                signals = sorted([*signals, "permission"])
            if signals:
                found[f"{module}.{name}"] = signals
    return found


def discover_role_calls() -> list[tuple[str, str, str]]:
    """Discover exact legacy-helper call contracts for manifest regeneration."""
    from apps.identity.current_context import (  # noqa: PLC0415
        require_current_actor_clinic_roles,
    )

    observed: list[tuple[str, str, str]] = []
    contract = signature(require_current_actor_clinic_roles)
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        aliases = {"require_current_actor_clinic_roles"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                aliases.update(
                    item.asname or item.name
                    for item in node.names
                    if item.name == "require_current_actor_clinic_roles"
                )
        for function in (
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ):
            for call in (
                node for node in ast.walk(function) if isinstance(node, ast.Call)
            ):
                if not isinstance(call.func, ast.Name) or call.func.id not in aliases:
                    continue
                assert all(keyword.arg is not None for keyword in call.keywords)
                bound = contract.bind(
                    *call.args,
                    **{
                        keyword.arg: keyword.value
                        for keyword in call.keywords
                        if keyword.arg is not None
                    },
                )
                roles_node = bound.arguments["roles"]
                if isinstance(roles_node, ast.Name):
                    assignments = [
                        statement.value
                        for statement in function.body
                        if isinstance(statement, ast.Assign)
                        and any(
                            isinstance(target, ast.Name) and target.id == roles_node.id
                            for target in statement.targets
                        )
                    ]
                    if assignments:
                        assert len(assignments) == 1
                        assert isinstance(assignments[0], ast.Tuple)
                        roles_node = assignments[0]
                assert isinstance(roles_node, ast.expr)
                observed.append(
                    (
                        str(path.relative_to(ROOT)),
                        function.name,
                        ast.unparse(roles_node),
                    )
                )
    return observed


def declared_probes() -> set[str]:
    """Collect the machine-consumed target ids, not inferred permission decisions."""
    result: set[str] = set()
    for path in Path(__file__).parent.glob("legacy_*boundaries.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Boundary"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                result.add(str(node.args[0].value))
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "VIEWS"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Dict)
            ):
                result.update(
                    str(key.value)
                    for key in node.value.keys
                    if isinstance(key, ast.Constant)
                )
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "TARGETS"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Tuple)
            ):
                result.update(
                    str(item.value)
                    for item in node.value.elts
                    if isinstance(item, ast.Constant)
                )
    return result
