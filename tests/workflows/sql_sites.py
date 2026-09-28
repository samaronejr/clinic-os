"""Derive every SQL decision in the workflows closure that reads the actor.

Sites are the workflow-owned functions, the RLS policies on workflow tables,
and each guarded ``RAISE`` branch of workflow trigger functions whose
expression reads ``app.current_user_id`` directly, through the trigger's
``actor`` variable, or through any helper/relation closure. The closure is
derived from the live catalog by todo 6's authority ``Catalog`` (function
bodies, dependencies, relation policies). Anything opaque or unparseable fails
closed. Identity-owned gates (``has_permission``) are dependencies with their
own oracle census, not sites here.

``mutated`` rewrites one site in a test database (force it to allow or deny)
and restores it byte-identically, verified by catalog digest.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.apps import apps
from django.db import connection

from identity.authority_catalog import Catalog
from identity.authority_sql import references
from identity.legacy_sql_inventory import migration_definitions

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

ACTOR = "app.current_user_id"
FUNCTION = "function"
POLICY = "policy"
BRANCH = "branch"
RAISE = re.compile(r"RAISE\s+EXCEPTION\s+'((?:[^']|'')*)'")
# The shared reference tokenizer reads a reserved word before "(" as a call.
# Reserved words cannot name an unquoted function, so only these are waived.
_TOKENIZER_KEYWORDS = frozenset(
    f"unresolved function {word}"
    for word in ("from", "where", "and", "or", "not", "in", "exists", "select", "is")
)
GUARD = re.compile(r"(?<!END )\b(?:ELS)?IF\b")


@dataclass(frozen=True)
class SqlSite:
    sid: str
    kind: str
    target: str
    boolean: bool = False
    span: tuple[int, int] = (0, 0)
    policy: tuple[str, str, bool, bool] = ("", "", False, False)


def _tables() -> list[str]:
    return sorted(
        model._meta.db_table for model in apps.get_app_config("workflows").get_models()
    )


def _functions() -> list[str]:
    return sorted(
        name
        for name, paths in migration_definitions().items()
        if any(path.startswith("apps/workflows/migrations/") for path in paths)
    )


def _branches(
    signature: str, source: str, catalog: Catalog, *, bypass: bool
) -> dict[str, SqlSite]:
    declared = re.search(r"\bactor\s+uuid\s*:=\s*([^;]+);", source)
    assert declared is not None, (signature, "trigger actor binding not found")
    assert ACTOR in catalog.statement("SELECT " + declared.group(1)).settings
    spans: list[tuple[int, int]] = []
    sites: dict[str, SqlSite] = {}
    for match in RAISE.finditer(source):
        before = source[: match.start()].rstrip()
        if before.endswith("ELSE"):
            continue
        assert before.endswith("THEN"), (signature, match.group(1), "unguarded RAISE")
        then = len(before) - len("THEN")
        guards = list(GUARD.finditer(source, 0, then))
        assert guards, (signature, match.group(1), "no IF for RAISE")
        start, end = guards[-1].end(), then
        condition = source[start:end]
        assert not re.search(r"\b(?:THEN|IF|LOOP)\b", condition), (signature, condition)
        spans.append((start, end))
        reads = catalog.statement("SELECT " + condition, bypass=bypass)
        opaque = {item for item in reads.opaque if item not in _TOKENIZER_KEYWORDS}
        assert not opaque, (signature, match.group(1), opaque)
        if ("actor",) in references(condition).names or ACTOR in reads.settings:
            base = f"{BRANCH}:{signature}:{match.group(1)}"
            sid = base if base not in sites else f"{base}#2"
            sites[sid] = SqlSite(
                sid, BRANCH, f"clinic_app.{signature}", span=(start, end)
            )
    readers = {
        f"clinic_app.{name}"
        for name in {m.group(1) for m in re.finditer(r"clinic_app\.(\w+)\(", source)}
        if ACTOR in catalog.statement(f"SELECT clinic_app.{name}()").settings
    }
    uses = [
        declared.end() + m.start()
        for m in re.finditer(r"\bactor\b", source[declared.end() :])
    ]
    uses += [
        m.start()
        for name in readers
        for m in re.finditer(re.escape(name) + r"\(", source)
    ]
    for position in uses:
        assert any(start <= position < end for start, end in spans), (
            signature,
            "actor read outside a classified decision",
            source[position : position + 60],
        )
    return sites


def discover_sql_sites() -> dict[str, SqlSite]:
    catalog = Catalog()
    tables = _tables()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.oid, p.oid::regprocedure::text, "
            "p.prorettype='boolean'::regtype AND NOT p.proretset, l.lanname, p.prosrc "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_language l ON l.oid=p.prolang "
            "WHERE n.nspname||'.'||p.proname=ANY(%s) ORDER BY 2",
            [_functions()],
        )
        functions = cursor.fetchall()
        cursor.execute(
            "SELECT DISTINCT t.tgfoid FROM pg_trigger t JOIN pg_class c "
            "ON c.oid=t.tgrelid WHERE c.relnamespace='clinic_app'::regnamespace "
            "AND c.relname=ANY(%s) AND NOT t.tgisinternal",
            [tables],
        )
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT c.relname, p.polname, pg_get_expr(p.polqual,p.polrelid), "
            "pg_get_expr(p.polwithcheck,p.polrelid) FROM pg_policy p "
            "JOIN pg_class c ON c.oid=p.polrelid "
            "WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s) "
            "ORDER BY 1,2",
            [tables],
        )
        policies = cursor.fetchall()
    assert triggers <= {row[0] for row in functions}, "foreign trigger function"
    sites: dict[str, SqlSite] = {}
    for oid, signature, boolean, language, source in functions:
        if oid in triggers:
            assert language == "plpgsql", signature
            trigger = catalog.functions[oid]
            # A SECURITY DEFINER trigger owned by a BYPASSRLS role reads raw rows.
            bypass = trigger.security_definer and trigger.bypass_rls
            sites.update(_branches(signature, source, catalog, bypass=bypass))
            continue
        reads = catalog._function(oid, bypass=False, seen=set())
        opaque = {item for item in reads.opaque if item not in _TOKENIZER_KEYWORDS}
        assert not opaque, (signature, opaque)
        if ACTOR in reads.settings:
            assert language == "sql", (signature, "uninspected language")
            sid = f"{FUNCTION}:{signature}"
            sites[sid] = SqlSite(
                sid, FUNCTION, f"clinic_app.{signature}", boolean=bool(boolean)
            )
    for table, name, using, check in policies:
        reads = catalog.statement(f"SELECT {using or 'true'}, {check or 'true'}")
        opaque = {item for item in reads.opaque if item not in _TOKENIZER_KEYWORDS}
        assert not opaque, (table, name, opaque)
        if ACTOR in reads.settings:
            sid = f"{POLICY}:{table}.{name}"
            sites[sid] = SqlSite(
                sid,
                POLICY,
                f"clinic_app.{table}",
                policy=(name, table, using is not None, check is not None),
            )
    return sites


def _digest(cursor: psycopg.Cursor[tuple[object, ...]]) -> str:
    cursor.execute(
        "SELECT string_agg(pg_get_functiondef(p.oid), '' ORDER BY p.oid) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='clinic_app' AND p.proname LIKE 'workflows\\_%%'"
    )
    functions = cursor.fetchone()
    cursor.execute(
        "SELECT string_agg(p.polname||coalesce(pg_get_expr(p.polqual,p.polrelid),'')"
        "||coalesce(pg_get_expr(p.polwithcheck,p.polrelid),''), '' "
        "ORDER BY p.polrelid, p.polname) FROM pg_policy p"
    )
    policies = cursor.fetchone()
    return hashlib.sha256(repr((functions, policies)).encode()).hexdigest()


def _replace_calls(body: str, names: tuple[str, ...], value: str) -> tuple[str, int]:
    count = 0
    for name in names:
        while (index := body.find(name + "(")) >= 0:
            depth, cursor = 0, index + len(name)
            while True:
                depth += {"(": 1, ")": -1}.get(body[cursor], 0)
                cursor += 1
                if depth == 0:
                    break
            body = body[:index] + value + body[cursor:]
            count += 1
    return body, count


def _statements(
    cursor: psycopg.Cursor[tuple[object, ...]], site: SqlSite, mode: str
) -> tuple[list[str], list[str]]:
    """Return (mutate, restore) statements for one site and mode."""
    value = "true" if mode == "allow" else "false"
    if site.kind == POLICY:
        name, _table, has_using, has_check = site.policy
        cursor.execute(
            "SELECT pg_get_expr(polqual,polrelid), pg_get_expr(polwithcheck,polrelid) "
            "FROM pg_policy WHERE polrelid=%s::regclass AND polname=%s",
            [site.target, name],
        )
        row = cursor.fetchone()
        assert row is not None
        clauses = [f"USING ({value})"] * has_using + [
            f"WITH CHECK ({value})"
        ] * has_check
        original = [f"USING ({row[0]})"] * has_using + [
            f"WITH CHECK ({row[1]})"
        ] * has_check
        return (
            [f"ALTER POLICY {name} ON {site.target} {' '.join(clauses)}"],
            [f"ALTER POLICY {name} ON {site.target} {' '.join(original)}"],
        )
    cursor.execute(
        "SELECT pg_get_functiondef(%s::regprocedure), prosrc FROM pg_proc "
        "WHERE oid=%s::regprocedure",
        [site.target, site.target],
    )
    row = cursor.fetchone()
    assert row is not None
    definition, source = str(row[0]), str(row[1])
    assert definition.count(source) == 1
    if site.kind == BRANCH:
        start, end = site.span
        # allow: the branch never refuses; deny: it always refuses.
        mutated = (
            source[:start]
            + f" {'false' if mode == 'allow' else 'true'} "
            + source[end:]
        )
    elif site.boolean:
        mutated = f"\n SELECT {value}\n"
    else:
        mutated, count = _replace_calls(
            source, ("clinic_app.has_permission", "clinic_app.workflows_owned"), value
        )
        assert count, site.sid
    return [definition.replace(source, mutated)], [definition]


@contextmanager
def mutated(
    connection_: psycopg.Connection[tuple[object, ...]], site: SqlSite, mode: str
) -> Iterator[None]:
    """Apply one SQL decision mutant as superuser; restore and verify by digest."""
    with connection_.cursor() as cursor:
        before = _digest(cursor)
        mutate, restore = _statements(cursor, site, mode)
        for statement in mutate:
            cursor.execute(statement)
        assert _digest(cursor) != before, site.sid
    try:
        yield
    finally:
        with connection_.cursor() as cursor:
            for statement in restore:
                cursor.execute(statement)
            assert _digest(cursor) == before, (site.sid, "restore digest mismatch")
