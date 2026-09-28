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


COMPARISON = re.compile(
    r"(?P<left>[A-Za-z_][\w.]*)\s*(?P<op>=|<>|!=|IS\s+NOT\s+DISTINCT\s+FROM|"
    r"IS\s+DISTINCT\s+FROM)\s*actor\b"
    r"|\bactor\s*(?P<rop>=|<>|!=)\s*(?P<right>[A-Za-z_][\w.]*)"
)
PERMISSION = re.compile(r"clinic_app\.has_permission\(\s*'([a-z.]+)'")


@dataclass(frozen=True)
class Condition:
    """One actor-reading operand of a trigger branch: a permission, helper or
    actor comparison. Removing it means treating it as satisfied."""

    cid: str
    branch: str
    kind: str
    target: str
    span: tuple[int, int]
    granted: str


def _call_end(text: str, start: int) -> int:
    depth, cursor = 0, text.index("(", start)
    while True:
        depth += {"(": 1, ")": -1}.get(text[cursor], 0)
        cursor += 1
        if depth == 0:
            return cursor


def discover_conditions() -> dict[str, Condition]:
    """Split every actor-reading trigger branch into its removable conditions.

    Fails closed when an ``actor`` token or an actor-reading helper call inside
    a branch is not covered by exactly one classified condition.
    """
    catalog = Catalog()
    conditions: dict[str, Condition] = {}
    with connection.cursor() as cursor:
        for site in discover_sql_sites().values():
            if site.kind != BRANCH:
                continue
            cursor.execute(
                "SELECT prosrc FROM pg_proc WHERE oid=%s::regprocedure", [site.target]
            )
            row = cursor.fetchone()
            assert row is not None
            source = str(row[0])
            start, end = site.span
            text = source[start:end]
            found: list[tuple[int, int, str, str]] = []
            for match in re.finditer(r"clinic_app\.(\w+)\(", text):
                name = match.group(1)
                if (
                    ACTOR
                    not in catalog.statement(f"SELECT clinic_app.{name}()").settings
                ):
                    continue
                stop = _call_end(text, match.start())
                permission = PERMISSION.match(text, match.start())
                kind = (
                    f"permission:{permission.group(1)}"
                    if permission
                    else f"helper:{name}"
                )
                found.append((match.start(), stop, kind, "true"))
            for match in COMPARISON.finditer(text):
                operator = " ".join((match.group("op") or match.group("rop")).split())
                granted = (
                    "true" if operator in {"=", "IS NOT DISTINCT FROM"} else "false"
                )
                found.append(
                    (
                        match.start(),
                        match.end(),
                        f"actor:{' '.join(match.group(0).split())}",
                        granted,
                    )
                )
            for use in re.finditer(r"\bactor\b", text):
                assert any(a <= use.start() < b for a, b, _k, _g in found), (
                    site.sid,
                    "unclassified actor condition",
                    text[use.start() - 40 : use.end() + 10],
                )
            for index, (a, b, kind, granted) in enumerate(sorted(found), 1):
                cid = f"{site.sid}#{index}:{kind}"
                conditions[cid] = Condition(
                    cid, site.sid, kind, site.target, (start + a, start + b), granted
                )
    return conditions


SCOPE_OPERAND = (
    r"(?:\b[A-Za-z_]\w*\.)?(?:clinic_id|organization_id)\b|\bclinic\b|\btenant\b"
)
SCOPE_COMPARISON = re.compile(
    r"(?P<left>(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)\s*"
    r"(?P<op>=|<>|!=|IS\s+NOT\s+DISTINCT\s+FROM|IS\s+DISTINCT\s+FROM)\s*"
    r"(?P<right>(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*|NULLIF\(\s*current_setting\("
    r"\s*'app\.current_tenant'\s*,\s*true\s*\)\s*,\s*''\s*\)::uuid)"
)
TENANT = "app.current_tenant"


def _is_scope(operand: str, parameters: set[str]) -> bool:
    name = operand.rsplit(".", maxsplit=1)[-1]
    return (
        name in {"clinic_id", "organization_id"}
        or ("." not in operand and name in parameters)
        or TENANT in operand
    )


def _spans(text: str, pattern: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(pattern, text)]


def _argument_spans(text: str) -> list[tuple[int, int]]:
    """Argument lists of calls: scope passed to a callee is forwarding."""
    spans = []
    for match in re.finditer(r"\b[A-Za-z_][\w.]*\(", text):
        if match.group(0)[:-1].upper() in {
            "IF",
            "AND",
            "OR",
            "NOT",
            "IN",
            "EXISTS",
            "ROW",
        }:
            continue
        spans.append((match.end(), _call_end(text, match.start()) - 1))
    return spans


def _code_only(source: str) -> str:
    """Blank comments and string literals (same length) so only code is scanned."""
    out = list(source)
    index = 0
    while index < len(source):
        if source.startswith("--", index):
            end = source.find("\n", index)
            end = len(source) if end < 0 else end
            out[index:end] = " " * (end - index)
            index = end
        elif source[index] == "'":
            end = index + 1
            while end < len(source):
                if source[end] == "'" and source[end + 1 : end + 2] == "'":
                    end += 2
                elif source[end] == "'":
                    break
                else:
                    end += 1
            out[index + 1 : end] = " " * (end - index - 1)
            index = end + 1
        else:
            index += 1
    return "".join(out)


def discover_scope_conditions() -> dict[str, Condition]:
    """Record scope in SQL: a row's clinic/org compared with the call's scope.

    Clinic comparisons are sites (removed and forced-mismatched by the sweep).
    Organization comparisons are the tenant boundary: rows carry composite
    (organization, clinic) keys, so each is implied by a clinic comparison or
    by has_permission's clinic-in-tenant check; they are classified, not
    mutated. Every other scope token must be forwarded as a call argument or
    projected; anything else fails closed.
    """
    sites: dict[str, Condition] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.oid::regprocedure::text, p.prosrc, "
            "coalesce(p.proargnames, ARRAY[]::text[]) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname||'.'||p.proname=ANY(%s) OR (n.nspname='clinic_app' "
            "AND p.proname LIKE 'workflows\\_%%') ORDER BY 1",
            [_functions()],
        )
        functions = cursor.fetchall()
    for signature, raw, names in functions:
        source = _code_only(raw)
        parameters = {name for name in names if name in {"clinic", "tenant"}}
        classified: list[tuple[int, int]] = []
        for match in SCOPE_COMPARISON.finditer(source):
            left, right = match.group("left"), match.group("right")
            if not (_is_scope(left, parameters) or _is_scope(right, parameters)):
                continue
            classified.append((match.start(), match.end()))
            text = " ".join(match.group(0).split())
            clinic = any(
                operand.split(".")[-1] == "clinic_id" or operand == "clinic"
                for operand in (left, right)
            ) and not any(
                operand.split(".")[-1] == "organization_id" or TENANT in operand
                for operand in (left, right)
            )
            if not clinic:
                continue
            operator = " ".join(match.group("op").split())
            base = f"scope:{signature}:{text}"
            cid = next(
                name
                for name in (base, *(f"{base}#{n}" for n in range(2, 99)))
                if name not in sites
            )
            sites[cid] = Condition(
                cid,
                signature,
                "scope",
                f"clinic_app.{signature}",
                (match.start(), match.end()),
                "true" if operator in {"=", "IS NOT DISTINCT FROM"} else "false",
            )
        classified += _argument_spans(source)
        classified += _spans(source, r"(?s)\bSELECT\b(?:(?!\bFROM\b).)*?\bFROM\b")
        for use in re.finditer(SCOPE_OPERAND, source):
            name = use.group(0).split(".")[-1]
            if name in {"clinic", "tenant"} and name not in parameters:
                continue
            assert any(a <= use.start() < b for a, b in classified), (
                signature,
                "unclassified record-scope use",
                source[max(0, use.start() - 50) : use.end() + 20],
            )
        for use in re.finditer(re.escape(TENANT), raw):
            assert any(a <= use.start() < b for a, b in classified), (
                signature,
                "unclassified tenant scope use",
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
    _require_no_actor_surfaces(catalog, tables)
    return sites


def _require_no_actor_surfaces(catalog: Catalog, tables: list[str]) -> None:
    """Fail closed on actor reads in constraints, defaults, indexes and views.

    None of these surfaces may decide on the actor in the workflows closure:
    there is no behavioural oracle for them, so any such expression (including
    one reached through a helper or relation) is refused as unclassified.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 'constraint '||c.relname||'.'||k.conname, "
            "pg_get_constraintdef(k.oid) FROM pg_constraint k "
            "JOIN pg_class c ON c.oid=k.conrelid "
            "WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s) "
            "AND k.conbin IS NOT NULL "
            "UNION ALL SELECT 'default '||c.relname||'.'||a.attname, "
            "pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
            "JOIN pg_class c ON c.oid=d.adrelid JOIN pg_attribute a "
            "ON a.attrelid=d.adrelid AND a.attnum=d.adnum "
            "WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s) "
            "UNION ALL SELECT 'index '||i.indexrelid::regclass::text, "
            "concat_ws(' ', pg_get_expr(i.indexprs,i.indrelid), "
            "pg_get_expr(i.indpred,i.indrelid)) FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indrelid "
            "WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s) "
            "AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL) "
            "UNION ALL SELECT DISTINCT 'view '||v.oid::regclass::text, "
            "pg_get_viewdef(v.oid) FROM pg_rewrite r JOIN pg_class v "
            "ON v.oid=r.ev_class JOIN pg_depend d ON d.classid='pg_rewrite'::regclass "
            "AND d.objid=r.oid JOIN pg_class c ON c.oid=d.refobjid "
            "WHERE v.relkind IN ('v','m') "
            "AND c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)",
            [tables, tables, tables, tables],
        )
        surfaces = cursor.fetchall()
    for label, expression in surfaces:
        text = str(expression)
        if text.upper().startswith("CHECK "):
            text = text[len("CHECK ") :].removesuffix(" NOT VALID")
        reads = catalog.statement(
            text if label.startswith("view ") else "SELECT " + text
        )
        opaque = {item for item in reads.opaque if item not in _TOKENIZER_KEYWORDS}
        assert not opaque, (label, opaque)
        assert ACTOR not in reads.settings, (
            "unclassified actor-reading surface",
            label,
        )


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
    cursor: psycopg.Cursor[tuple[object, ...]],
    site: SqlSite | tuple[Condition, ...],
    mode: str,
) -> tuple[list[str], list[str]]:
    """Return (mutate, restore) statements for one site and mode."""
    if isinstance(site, tuple):
        # Remove several conditions of one trigger at once (reviewer's X3).
        assert len({condition.target for condition in site}) == 1
        cursor.execute(
            "SELECT pg_get_functiondef(%s::regprocedure), prosrc FROM pg_proc "
            "WHERE oid=%s::regprocedure",
            [site[0].target, site[0].target],
        )
        row = cursor.fetchone()
        assert row is not None
        definition, source = str(row[0]), str(row[1])
        assert definition.count(source) == 1
        mutated = source
        for condition in sorted(site, key=lambda item: item.span, reverse=True):
            a, b = condition.span
            mutated = mutated[:a] + f"({condition.granted})" + mutated[b:]
        return [definition.replace(source, mutated)], [definition]
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
    connection_: psycopg.Connection[tuple[object, ...]],
    site: SqlSite | tuple[Condition, ...],
    mode: str,
) -> Iterator[None]:
    """Apply one SQL decision mutant as superuser; restore and verify by digest."""
    with connection_.cursor() as cursor:
        before = _digest(cursor)
        mutate, restore = _statements(cursor, site, mode)
        for statement in mutate:
            cursor.execute(statement)
        assert _digest(cursor) != before, getattr(site, "sid", site)
    try:
        yield
    finally:
        with connection_.cursor() as cursor:
            for statement in restore:
                cursor.execute(statement)
            assert _digest(cursor) == before, (repr(site), "restore digest mismatch")
