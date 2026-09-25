"""Independent migration-source and deployed-catalog authorization census.

All SECURITY DEFINER functions are roots, as are functions used by RLS policies.
SQL string bodies do not reliably create pg_depend edges, so resolver calls are
also followed conservatively by name (including every overload). No name-based
exclusion silently discards a migration-defined guard.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

from django.db import connection

ROOT = Path(__file__).resolve().parents[2]

DEFINITION = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"
    r"((?:clinic_app|public)\.[a-z_][a-z_0-9]*)\s*\(",
    re.IGNORECASE,
)
CALL = re.compile(
    r"(?<![\w.])(?:(clinic_app|public)\.)?([a-z_][a-z_0-9]*)\s*\(", re.IGNORECASE
)


class SqlSignature(TypedDict):
    arguments: str
    returns: str
    set_returning: bool
    security_definer: bool


class SqlDiscovery(TypedDict):
    signatures: list[SqlSignature]
    migrations: list[str]
    policies: list[str]
    resolvers: list[str]


class SqlInventoryEntry(TypedDict):
    discovery: SqlDiscovery
    kind: str
    probes: list[str]
    reason: str


def assert_sql_inventory(entries: dict[str, SqlInventoryEntry]) -> None:
    """A missing catalog guard is an error even without a Python caller."""
    assert discover_sql() == {
        name: entry["discovery"] for name, entry in entries.items()
    }


def migration_definitions() -> dict[str, list[str]]:
    """Scan SQL in migrations AND older migration support modules outside them."""
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted((ROOT / "apps").rglob("*.py")):
        for name in sorted(set(DEFINITION.findall(path.read_text()))):
            found[name.lower()].append(str(path.relative_to(ROOT)))
    return dict(found)


def _body_callees(body: str, by_name: dict[str, set[int]]) -> set[int]:
    found: set[int] = set()
    for schema, name in CALL.findall(body):
        names = (
            [f"{schema}.{name}"] if schema else [f"clinic_app.{name}", f"public.{name}"]
        )
        for qualified in names:
            found.update(by_name.get(qualified, set()))
    return found


def discover_sql() -> dict[str, SqlDiscovery]:
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT p.oid, n.nspname || '.' || p.proname,
                   pg_get_function_identity_arguments(p.oid), t.typname,
                   p.proretset, p.prosecdef, p.prosrc
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_type t ON t.oid=p.prorettype
            WHERE n.nspname IN ('clinic_app','public') AND p.prokind='f'
            ORDER BY 2,3
        """)
        functions = {int(row[0]): row for row in cursor.fetchall()}
        cursor.execute("""
            SELECT d.refobjid, n.nspname || '.' || c.relname || ':' || p.polname
            FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_depend d ON d.classid='pg_policy'::regclass AND d.objid=p.oid
            WHERE d.refclassid='pg_proc'::regclass
              AND n.nspname IN ('clinic_app','public')
            ORDER BY 2
        """)
        policy_rows = cursor.fetchall()
        cursor.execute("""
            SELECT objid, refobjid FROM pg_depend
            WHERE classid='pg_proc'::regclass AND refclassid='pg_proc'::regclass
        """)
        dependency_rows = cursor.fetchall()
    dependencies: dict[int, set[int]] = defaultdict(set)
    for caller, callee in dependency_rows:
        if caller in functions and callee in functions:
            dependencies[caller].add(callee)
    by_name: dict[str, set[int]] = defaultdict(set)
    for oid, row in functions.items():
        by_name[str(row[1])].add(oid)
    policies: dict[int, set[str]] = defaultdict(set)
    for oid, policy in policy_rows:
        if oid in functions:
            policies[oid].add(str(policy))
    sources = migration_definitions()
    selected = {
        oid for oid, row in functions.items() if row[5] or row[1] in sources
    } | set(policies)
    callers: dict[int, set[str]] = defaultdict(set)
    pending = list(selected)
    while pending:
        oid = pending.pop()
        row = functions[oid]
        callees = dependencies[oid] | _body_callees(str(row[6]), by_name)
        for callee in callees:
            callers[callee].add(str(row[1]))
            if callee not in selected:
                selected.add(callee)
                pending.append(callee)
    result: dict[str, SqlDiscovery] = {}
    for name in sorted(set(sources) | {str(functions[oid][1]) for oid in selected}):
        ids = sorted(by_name.get(name, set()) & selected)
        result[name] = {
            "signatures": sorted(
                [
                    {
                        "arguments": str(functions[oid][2]),
                        "returns": str(functions[oid][3]),
                        "set_returning": bool(functions[oid][4]),
                        "security_definer": bool(functions[oid][5]),
                    }
                    for oid in ids
                ],
                key=lambda item: item["arguments"],
            ),
            "migrations": sources.get(name, []),
            "policies": sorted({p for oid in ids for p in policies[oid]}),
            "resolvers": sorted({p for oid in ids for p in callers[oid]}),
        }
    return result
