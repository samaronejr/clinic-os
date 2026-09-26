"""Every channel through which executing code can observe the staff actor.

An exemption claims "no staff permission gate here". The executed differential
matrix can only certify the staff states it builds, and the space of states is
exponential (every subset of role-permission removals, every user flag, every
assignment). So the census makes the claim sound by construction: an exempt
function must not observe the actor AT ALL. Code that never reads the actor
cannot decide anything about it, whatever the actor's roles, grants, flags or
assignments are.

In this system the staff actor exists in one place only: the settings that
``tenant_context`` binds to the actor (derived live by ``actor_settings``). No
Python context variable or thread-local carries it. It can therefore be
observed only through:

- SQL that reads an actor setting, whether the statement comes from Python or
  from a function body;
- a ``clinic_app`` function that transitively reads one (``has_permission``,
  ``load_current_user`` and every helper and trigger function built on them);
- a relation whose row policy for the runtime role reads one, directly, through
  such a function, or through another such relation (the policy runs on every
  scan);
- invoker-rights functions and views that read such a relation;
- the request's authenticated user, for code handed a staff request.

``actor_catalog`` derives the function and relation sets from ``pg_proc``,
``pg_policy`` and the view definitions to a fixed point. It fails closed on what
it cannot observe at run time: an actor-reading SQL function the planner may
inline (it would leave no call in the function statistics), dynamic SQL in a
function body, and opaque non-extension languages. ``python_accessors`` derives
the ``apps.identity`` functions that reach the actor from the census reference
graph. ``ActorObserver`` records, per execution:
- exact call deltas of actor functions from ``pg_stat_xact_user_functions``
  (``track_functions = all``);
- every statement Python sends, with the Python stack that sent it;
- relation touch deltas from ``pg_stat_xact_user_tables``, where a touch of an
  actor relation that no statement or called function explains fails closed;
- entry into an accessor (``sys.monitoring``), with an accessor outside the
  derived set failing closed;
- reads of a staff request's ``user``.
"""

from __future__ import annotations

import ast
import re
import sys
import traceback
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import psycopg
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from psycopg import sql

from identity.permission_gate_census import CensusError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from types import CodeType
    from uuid import UUID

    from django.db.backends.utils import CursorWrapper
    from django.http import HttpRequest

    from identity.permission_gate_census import Graph

_TOKEN: Final = re.compile(r"[a-z_][a-z0-9_$]*")
_LITERAL: Final = re.compile(r"'(?:[^']|'')*'")
_COMMENT: Final = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_DYNAMIC: Final = re.compile(r"\bexecute\b")
# The runtime role and the owner role (owner-only paths run as it).
RUNTIME_ROLES: Final = ("clinic_app", "clinic_owner")


_SET_CONFIG: Final = re.compile(r"set_config\(\s*'([^']+)'\s*,\s*%s", re.IGNORECASE)


def actor_settings(
    bind: Callable[[], AbstractContextManager[None]], actor: UUID
) -> frozenset[str]:
    """Settings the staff scope binds to the actor, captured as it binds them.

    Every ``set_config`` the scope sends is recorded; a setting counts when
    its bound value is the actor and it still reads back as the actor.
    """
    sent: list[tuple[str, object]] = []

    def capture(
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        sent.append((sql, params))
        return execute(sql, params, many, context)

    found: set[str] = set()
    with connection.execute_wrapper(capture), bind():
        for sql, params in sent:
            values = list(params) if isinstance(params, list | tuple) else []
            for match in _SET_CONFIG.finditer(sql):
                index = sql[: match.end()].count("%s") - 1
                if index < len(values) and str(values[index]) == str(actor):
                    found.add(match.group(1))
        with connection.cursor() as cursor:
            for setting in sorted(found):
                cursor.execute("SELECT pg_catalog.current_setting(%s, true)", [setting])
                assert cursor.fetchone() == (str(actor),), setting
    return frozenset(found)


def enable_function_statistics(superuser_url: str) -> None:
    """Count function calls on this connection (``track_functions = all``).

    The setting is superuser-only; the superuser grants SET on it to the
    session role for the one statement and revokes it again.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT session_user")
        row = cursor.fetchone()
    assert row is not None
    grant = sql.SQL("{} SET ON PARAMETER track_functions {} {}")
    role = sql.Identifier(str(row[0]))
    with psycopg.connect(superuser_url, autocommit=True) as superuser:
        superuser.execute(grant.format(sql.SQL("GRANT"), sql.SQL("TO"), role))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET track_functions = 'all'")
    finally:
        with psycopg.connect(superuser_url, autocommit=True) as superuser:
            superuser.execute(grant.format(sql.SQL("REVOKE"), sql.SQL("FROM"), role))


@dataclass(frozen=True, slots=True)
class ActorCatalog:
    """Where the actor can be observed inside the database."""

    settings: frozenset[str]
    functions: Mapping[int, str]
    relations: frozenset[str]
    reads: Mapping[int, frozenset[str]]
    names: Mapping[int, str]
    relation_names: frozenset[str]


def _code(text: str) -> str:
    return _COMMENT.sub(" ", text.lower())


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(_LITERAL.sub(" ", _code(text))))


def _reads_setting(text: str, settings: frozenset[str]) -> bool:
    """The text names an actor setting (quoted or not)."""
    lowered = _code(text)
    return any(
        re.search(rf"(?<![\w.]){re.escape(setting)}(?![\w.])", lowered)
        for setting in settings
    )


def _binding(setting: str) -> re.Pattern[str]:
    name = re.escape(setting)
    return re.compile(
        rf"set_config\s*\(\s*'{name}'|\bset\s+(?:local\s+|session\s+)?{name}\b",
        re.IGNORECASE,
    )


def actor_catalog(cursor: CursorWrapper, settings: frozenset[str]) -> ActorCatalog:
    """Derive actor-observing functions and relations to a fixed point."""
    assert settings, "no setting carries the actor"
    cursor.execute(
        "SELECT p.oid, p.proname, l.lanname, p.prosrc, p.prosecdef, "
        "r.rolname, r.rolbypassrls, p.proconfig IS NOT NULL, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
        "  WHERE d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass "
        "  AND d.objid = p.oid AND d.deptype = 'e') "
        "FROM pg_catalog.pg_proc p "
        "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_catalog.pg_language l ON l.oid = p.prolang "
        "JOIN pg_catalog.pg_roles r ON r.oid = p.proowner "
        "WHERE n.nspname = 'clinic_app'"
    )
    functions = cursor.fetchall()
    cursor.execute(
        "SELECT c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity, "
        "pg_catalog.pg_get_userbyid(c.relowner), "
        "CASE WHEN c.relkind IN ('v', 'm') "
        "  THEN pg_catalog.pg_get_viewdef(c.oid) ELSE '' END "
        "FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'clinic_app' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
    )
    relations = {str(row[0]): row for row in cursor.fetchall()}
    cursor.execute(
        "SELECT c.relname, "
        "COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), '') || ' ' || "
        "COALESCE(pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), ''), "
        "ARRAY(SELECT CASE WHEN r = 0 THEN 'public' "
        "  ELSE pg_catalog.pg_get_userbyid(r) END FROM unnest(p.polroles) r) "
        "FROM pg_catalog.pg_policy p "
        "JOIN pg_catalog.pg_class c ON c.oid = p.polrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'clinic_app'"
    )
    policies = [
        (str(table), str(expression), set(roles))
        for table, expression, roles in cursor.fetchall()
    ]
    names = {int(row[0]): str(row[1]) for row in functions}
    reads: dict[int, frozenset[str]] = {}
    for oid, _name, language, body, *_ in functions:
        if language in ("sql", "plpgsql"):
            reads[int(oid)] = frozenset(_tokens(str(body)) & set(relations))
    actor: dict[int, str] = {}
    actor_relations: set[str] = set()
    while _grow_relations(policies, relations, actor, actor_relations, settings) | (
        _grow_functions(functions, actor, actor_relations, settings)
    ):
        pass
    _fail_closed(functions, actor, settings)
    return ActorCatalog(
        settings,
        actor,
        frozenset(actor_relations),
        reads,
        names,
        frozenset(relations),
    )


def _grow_relations(
    policies: Iterable[tuple[str, str, set[str]]],
    relations: Mapping[str, tuple[object, ...]],
    actor: Mapping[int, str],
    actor_relations: set[str],
    settings: frozenset[str],
) -> bool:
    """Relations whose runtime-role policy, or view body, observes the actor."""
    before = len(actor_relations)
    called = set(actor.values())
    for table, expression, roles in policies:
        if roles & {"public", *RUNTIME_ROLES} and (
            _reads_setting(expression, settings)
            or _tokens(expression) & (called | actor_relations)
        ):
            actor_relations.add(table)
    for name, row in relations.items():
        definition = str(row[-1])
        if row[1] in ("v", "m") and (
            _reads_setting(definition, settings)
            or _tokens(definition) & (called | actor_relations)
        ):
            actor_relations.add(name)
    return len(actor_relations) > before


def _grow_functions(
    functions: Iterable[tuple[object, ...]],
    actor: dict[int, str],
    actor_relations: set[str],
    settings: frozenset[str],
) -> bool:
    """Functions that read an actor setting, call an actor function, or read
    an actor relation where its policies apply (not a bypassing definer)."""
    before = len(actor)
    for oid, name, language, body, definer, _owner, bypass, *_rest in functions:
        if int(str(oid)) in actor or language not in ("sql", "plpgsql"):
            continue
        tokens = _tokens(str(body))
        sees_policies = not (definer and bypass)
        if (
            _reads_setting(str(body), settings)
            or tokens & set(actor.values())
            or (sees_policies and tokens & actor_relations)
        ):
            actor[int(str(oid))] = str(name)
    return len(actor) > before


def _fail_closed(
    functions: Iterable[tuple[object, ...]],
    actor: Mapping[int, str],
    settings: frozenset[str],
) -> None:
    for (
        oid,
        name,
        language,
        body,
        definer,
        _owner,
        _bypass,
        configured,
        ext,
    ) in functions:
        where = f"clinic_app.{name}"
        if language not in ("sql", "plpgsql", "c", "internal") or (
            language in ("c", "internal") and not ext
        ):
            message = f"{where} is opaque ({language}); its actor reads are unknown"
            raise CensusError(message)
        if language in ("sql", "plpgsql") and any(
            _binding(setting).search(str(body)) for setting in settings
        ):
            message = (
                f"{where} binds an actor setting; a deployed context without an "
                "actor could acquire one inside the database"
            )
            raise CensusError(message)
        if language == "plpgsql" and _DYNAMIC.search(
            _LITERAL.sub(" ", _code(str(body)))
        ):
            message = f"{where} uses dynamic SQL; its actor reads are unknown"
            raise CensusError(message)
        if (
            int(str(oid)) in actor
            and language == "sql"
            and not definer
            and not configured
        ):
            message = (
                f"{where} reads the actor ({sorted(settings)}) and may be inlined, "
                "so its calls cannot be counted"
            )
            raise CensusError(message)


def python_accessors(graph: Graph, catalog: ActorCatalog) -> dict[str, str]:
    """``apps.identity`` functions that reach the actor, with the reason.

    Seeds are functions whose SQL literals read an actor setting or call an
    actor function; the set closes over callers in the census reference
    graph.
    """
    seeds = _accessor_seeds(graph, catalog)
    callers: dict[str, set[str]] = {}
    for source, targets in graph.edges.items():
        for target in targets:
            callers.setdefault(target, set()).add(source)
    reached = dict(seeds)
    frontier = list(seeds)
    while frontier:
        current = frontier.pop()
        for caller in callers.get(current, ()):
            if caller not in reached:
                reached[caller] = f"calls {current}"
                frontier.append(caller)
    return {
        symbol: reason
        for symbol, reason in reached.items()
        if symbol.startswith("apps.identity.")
    }


def _accessor_seeds(graph: Graph, catalog: ActorCatalog) -> dict[str, str]:
    """Functions whose own SQL literals read the actor or call an actor function."""
    functions = set(catalog.functions.values())
    seeds: dict[str, str] = {}
    for module in graph.modules.values():
        for qualname, node in module.defs.items():
            texts = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            ]
            for text in texts:
                called = _tokens(text) & functions
                if _reads_setting(text, catalog.settings):
                    seeds[f"{module.name}.{qualname}"] = "reads an actor setting"
                elif called:
                    seeds[f"{module.name}.{qualname}"] = "calls " + ", ".join(
                        sorted(called)
                    )
    return seeds


class _ObservedUser:
    """A data descriptor that records every read of a staff request's user."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, request: object, owner: type | None = None) -> object:
        if request is None:
            return self
        user = request.__dict__["_observed_user"]
        if not isinstance(user, AnonymousUser):
            _READS.append("reads the staff request's user")
        return user

    def __set__(self, request: object, value: object) -> None:
        request.__dict__["_observed_user"] = value


_READS: list[str] = []
_OBSERVED_CLASSES: dict[type, type] = {}


def observe_request_user(request: HttpRequest) -> HttpRequest:
    """Make ``request.user`` reads visible to the observer."""
    base = type(request)
    if base not in _OBSERVED_CLASSES:
        _OBSERVED_CLASSES[base] = type(
            f"Observed{base.__name__}", (base,), {"user": _ObservedUser()}
        )
    user = request.__dict__.pop("user", None)
    request.__class__ = _OBSERVED_CLASSES[base]
    request.__dict__["_observed_user"] = user
    return request


@dataclass(slots=True)
class _Statement:
    sql: str
    params: tuple[object, ...]
    stack: tuple[str, ...]


@dataclass(slots=True)
class ActorObserver:
    """Collect actor observations for one execution at a time.

    ``begin(bound=True)``: an actor is bound (deployed staff context); every
    channel counts. ``begin(bound=False)``: the deployed context binds no
    actor (patient session, sessionless entry); every actor channel then
    reads the same unbound value, so what counts is acquiring an actor:
    a statement binding an actor setting, or one still bound at the end.
    Reads of a staff request's user count in both modes.
    """

    catalog: ActorCatalog
    accessors: Mapping[CodeType, str]
    statements: list[_Statement] = field(default_factory=list)
    entered: list[str] = field(default_factory=list)
    active: bool = False
    bound: bool = True
    _functions: dict[int, int] = field(default_factory=dict)
    _tables: dict[str, int] = field(default_factory=dict)

    def _snapshot(self) -> tuple[dict[int, int], dict[str, int]]:
        self.active = False
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT funcid, calls FROM pg_catalog.pg_stat_xact_user_functions"
            )
            functions = {int(oid): int(calls) for oid, calls in cursor.fetchall()}
            cursor.execute(
                "SELECT relname, seq_scan + COALESCE(idx_scan, 0) + n_tup_ins "
                "+ n_tup_upd + n_tup_del "
                "FROM pg_catalog.pg_stat_xact_user_tables "
                "WHERE schemaname = 'clinic_app'"
            )
            tables = {str(name): int(count) for name, count in cursor.fetchall()}
        return functions, tables

    def _unbound(self) -> bool:
        self.active = False
        with connection.cursor() as cursor:
            for setting in sorted(self.catalog.settings):
                cursor.execute("SELECT pg_catalog.current_setting(%s, true)", [setting])
                if cursor.fetchone() not in ((None,), ("",)):
                    return False
        return True

    def begin(self, *, bound: bool) -> None:
        self.bound = bound
        if bound:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_catalog.current_setting('track_functions')")
                assert cursor.fetchone() == ("all",), "track_functions must be all"
            self._functions, self._tables = self._snapshot()
        else:
            assert self._unbound(), "the deployed context already binds an actor"
        self.statements.clear()
        self.entered.clear()
        _READS.clear()
        self.active = True

    def end(self) -> list[str]:
        found: list[str] = [
            f"binds an actor setting from {statement.stack[:1]}"
            for statement in self.statements
            if self._binds(statement)
        ]
        if self.bound:
            found.extend(self._bound_findings())
        elif not self._unbound():
            found.append("leaves an actor bound")
        found.extend(sorted(set(_READS)))
        _READS.clear()
        self.active = False
        return sorted(set(found))

    def _binds(self, statement: _Statement) -> bool:
        for setting in self.catalog.settings:
            match = _binding(setting).search(statement.sql)
            if match is None:
                continue
            placeholders = statement.sql[: match.end()].count("%s")
            follows = statement.sql[match.end() :].lstrip(" ,")
            if follows.startswith("%s") and placeholders < len(statement.params):
                value = statement.params[placeholders]
                return value not in (None, "")
            return True
        return False

    def _bound_findings(self) -> list[str]:
        functions, tables = self._snapshot()
        found: list[str] = []
        called = {
            oid
            for oid, calls in functions.items()
            if calls > self._functions.get(oid, 0)
        }
        found.extend(
            f"calls clinic_app.{self.catalog.functions[oid]}"
            for oid in sorted(called & set(self.catalog.functions))
        )
        explained: set[str] = set()
        for oid in called:
            explained |= self.catalog.reads.get(oid, frozenset())
        actor_functions = set(self.catalog.functions.values())
        listed = set(self.accessors.values())
        for statement in self.statements:
            text = statement.sql
            tokens = _tokens(text)
            explained |= tokens & self.catalog.relation_names
            reads = _reads_setting(text, self.catalog.settings)
            if reads:
                found.append(
                    f"statement reads an actor setting from {statement.stack[:1]}"
                )
            found.extend(
                f"statement calls clinic_app.{name}"
                for name in sorted(tokens & actor_functions)
            )
            found.extend(
                f"statement touches actor relation {relation}"
                for relation in sorted(tokens & self.catalog.relations)
            )
            sender = next(
                (name for name in statement.stack if name.startswith("apps.identity.")),
                "",
            )
            if sender and (reads or tokens & actor_functions) and sender not in listed:
                found.append(f"unlisted accessor {sender} reaches the actor")
        touched = {
            name for name, count in tables.items() if count > self._tables.get(name, 0)
        }
        found.extend(
            f"unexplained touch of actor relation {name}"
            for name in sorted((touched & self.catalog.relations) - explained)
        )
        found.extend(f"enters {name}" for name in self.entered)
        return found

    def capture(
        self,
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        if self.active:
            values = tuple(params) if isinstance(params, list | tuple) else ()
            self.statements.append(_Statement(sql, values, _apps_stack()))
        return execute(sql, params, many, context)

    def on_start(self, code: CodeType) -> None:
        if self.active and code in self.accessors:
            self.entered.append(self.accessors[code])


def _apps_stack() -> tuple[str, ...]:
    """Qualified names of the apps.* frames sending a statement, innermost first."""
    names: list[str] = []
    for frame, _ in traceback.walk_stack(sys._getframe(1)):
        module = frame.f_globals.get("__name__", "")
        if isinstance(module, str) and module.startswith("apps."):
            names.append(f"{module}.{frame.f_code.co_qualname}")
    return tuple(names)


@contextmanager
def statements_captured(observer: ActorObserver) -> Iterator[None]:
    with connection.execute_wrapper(observer.capture):
        yield
