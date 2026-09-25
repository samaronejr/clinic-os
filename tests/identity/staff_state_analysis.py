"""Resolve staff-state reads independently of inventory labels and spellings.

This is conservative source analysis, not execution of an authorization guard.
Aliases, related ORM lookups, SQL constants and application call edges retain
provenance. A nonstaff claim must have no reachable membership/role read.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import connection

from identity.legacy_sql_inventory import discover_sql

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# Stored authority, including Django group/permission memberships. Enumeration
# constants alone are not state reads; queries on their managers are.
STAFF_TABLE = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:ONLY\s+)?(?:[a-z_]+\.)?"
    r"(identity_userclinicrole|identity_rolegrant|identity_careteammembership|"
    r"(?:identity_user|auth_user)_(?:groups|user_permissions)|auth_group(?:_permissions)?|auth_permission)\b",
    re.IGNORECASE,
)
MODELS = {"UserClinicRole", "RoleGrant", "CareTeamMembership", "Group", "Permission"}
MANAGERS = {"objects", "_default_manager", "_base_manager"}
RELATIONS = (
    "userclinicrole",
    "rolegrant",
    "careteammembership",
    "groups",
    "user_permissions",
)
ROLE_HELPERS = {
    "require_current_actor_clinic_roles",
    "require_current_actor_org_admin",
    "has_clinic_role",
    "clinics_for_user_roles",
}
ROLE_PROPERTIES = {"_has_role", "is_physician_anywhere", "is_clinic_admin_anywhere"}
SQL_CALL = re.compile(
    r"\b((?:clinic_app|public)\.[a-z_][a-z_0-9]*)\s*\(", re.IGNORECASE
)


def _functions(nodes: list[ast.stmt], prefix: str = "") -> dict[str, ast.FunctionDef]:
    found = {}
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            found.update(_functions(node.body, prefix + node.name + "."))
        elif isinstance(node, ast.FunctionDef):
            found[prefix + node.name] = node
    return found


def _name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return _name(node.value, aliases) + "." + node.attr
    if isinstance(node, ast.Call):
        # Preserve a queryset/model/instance provenance through calls.
        return _name(node.func, aliases)
    return ""


def _imports(tree: ast.Module, module: str) -> dict[str, str]:
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = (
                    item.name if item.asname else item.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(
                    module.split(".")[: -node.level] + ([base] if base else [])
                )
            for item in node.names:
                aliases[item.asname or item.name] = base + "." + item.name
    return aliases


def _bindings(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value
                resolved = _name(value, aliases)
                if resolved:
                    aliases[target.id] = resolved
    return values


def _strings(
    node: ast.AST, values: dict[str, ast.AST], seen: frozenset[str] = frozenset()
) -> str:
    fragments = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            fragments.append(child.value)
        elif (
            isinstance(child, ast.Name) and child.id in values and child.id not in seen
        ):
            fragments.append(_strings(values[child.id], values, seen | {child.id}))
    return " ".join(fragments) + "\n" + "".join(fragments)


@dataclass
class StaffAnalysis:
    direct: dict[str, set[str]] = field(default_factory=dict)
    calls: dict[str, set[str]] = field(default_factory=dict)

    def evidence(self, symbol: str) -> set[str]:
        pending = [symbol]
        visited: set[str] = set()
        found: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            found.update(self.direct.get(current, set()))
            pending.extend(self.calls.get(current, set()))
        return found

    def reachable(self, symbol: str) -> set[str]:
        pending = list(self.calls.get(symbol, set()))
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current not in visited:
                visited.add(current)
                pending.extend(self.calls.get(current, set()))
        return visited


def _sql_calls(text: str) -> set[str]:
    return {
        match[1]
        for match in SQL_CALL.finditer(text)
        if not re.search(
            r"\bFUNCTION\s+(?:IF\s+EXISTS\s+)?$", text[: match.start()], re.IGNORECASE
        )
    }


def _call_reads(child: ast.Call, aliases: dict[str, str]) -> set[str]:
    found = set()
    name = _name(child.func, aliases)
    parts = name.split(".")
    if parts[-1] in ROLE_HELPERS or parts[-1] in ROLE_PROPERTIES:
        found.add(f"role helper {name}")
    if set(parts) & MODELS and set(parts) & MANAGERS and parts[-1] != "create":
        found.add(f"membership query {name}")
    if name.endswith(".get_model"):
        names = [str(arg.value) for arg in child.args if isinstance(arg, ast.Constant)]
        if any(
            name.split(".")[-1].lower() in {m.lower() for m in MODELS} for name in names
        ):
            found.add("membership model lookup")
    return found


def _reads(node: ast.FunctionDef, aliases: dict[str, str], text: str) -> set[str]:
    found = {
        f"SQL table {table.lower()}"
        for table in STAFF_TABLE.findall(text.replace('"', ""))
    }
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            found.update(_call_reads(child, aliases))
        elif isinstance(child, ast.Attribute):
            if (
                child.attr in ROLE_PROPERTIES
                or child.attr.removesuffix("_set") in RELATIONS
            ):
                found.add(f"role/membership property {child.attr}")
        elif isinstance(child, ast.keyword) and child.arg:
            if any(part in RELATIONS for part in child.arg.split("__")[:-1]):
                found.add(f"membership lookup {child.arg}")
        elif (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and any(part in RELATIONS for part in child.value.split("__")[:-1])
        ):
            found.add(f"membership lookup {child.value}")
    return found


def python_staff_analysis(root: Path) -> StaffAnalysis:
    analysis = StaffAnalysis()
    for path in sorted((root / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        module = str(path.relative_to(root))[:-3].replace("/", ".")
        tree = ast.parse(path.read_text())
        functions = _functions(tree.body)
        imported = _imports(tree, module)
        imported.update(
            {name: f"{module}.{name}" for name in functions if "." not in name}
        )
        imported.update(
            {
                node.name: f"{module}.{node.name}"
                for node in tree.body
                if isinstance(node, ast.ClassDef)
            }
        )
        globals_ = _bindings(tree.body, imported)
        for name in globals_:
            imported.setdefault(name, module + "." + name)
        for name, value in globals_.items():
            text = _strings(value, globals_)
            symbol = module + "." + name
            analysis.direct[symbol] = {
                f"SQL table {table.lower()}"
                for table in STAFF_TABLE.findall(text.replace('"', ""))
            }
            analysis.calls[symbol] = _sql_calls(text)
        for name, node in functions.items():
            symbol = module + "." + name
            aliases = dict(imported)
            if "." in name:
                aliases["self"] = module + "." + name.rsplit(".", 1)[0]
            values = {**globals_, **_bindings(list(ast.walk(node)), aliases)}
            text = _strings(node, values)
            analysis.direct[symbol] = _reads(node, aliases, text)
            calls = {
                _name(n.func, aliases)
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
            }
            calls.update(_sql_calls(text))
            calls.update(
                _name(n, aliases) for n in ast.walk(node) if isinstance(n, ast.Name)
            )
            analysis.calls[symbol] = calls
    return analysis


def add_sql_staff_analysis(analysis: StaffAnalysis) -> None:
    """Inspect deployed bodies, including parsed SQL bodies and SQL call edges."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT n.nspname || '.' || p.proname, pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname IN ('clinic_app','public') AND p.prokind='f'
        """)
        rows = cursor.fetchall()
    for name, body in rows:
        analysis.direct.setdefault(name, set()).update(
            f"SQL table {table.lower()}"
            for table in STAFF_TABLE.findall(body.replace('"', ""))
        )
        analysis.calls.setdefault(name, set()).update(_sql_calls(body) - {name})
    # pg_depend covers parsed SQL bodies; the independent catalog census also
    # resolves unqualified calls in string bodies (including all overloads).
    for callee, discovered in discover_sql().items():
        for caller in discovered["resolvers"]:
            analysis.calls.setdefault(caller, set()).add(callee)
