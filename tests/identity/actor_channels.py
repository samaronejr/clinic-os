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

- a session-setting read, whether the statement comes from Python or from a
  function body. The readers are derived (``setting_readers``: the backend's
  setting builtins and the catalog views over them). Every use has its name
  resolved at run time from literals and bound parameters; a name that
  resolves to an actor setting, a name that cannot be resolved (computed in
  SQL, a column, a variable), and every enumerating reader (``pg_settings``,
  ``SHOW ALL``) count. Spelling does not matter;
- a ``clinic_app`` function that transitively reads one (``has_permission``,
  ``load_current_user`` and every helper and trigger function built on them);
- a relation whose row policy for the runtime role reads one, directly, through
  such a function, or through another such relation (the policy runs on every
  scan);
- invoker-rights functions and views that read such a relation;
- the request's authenticated user, for code handed a staff request.

Behaviour backs the reading rules: with an actor bound, the actor's id showing
up in any statement's parameters, in any result row, or in the outcome counts
as an observation however it was obtained.

``actor_catalog`` derives the function and relation sets from ``pg_proc``,
``pg_policy`` and the view definitions to a fixed point, over every
non-system schema. It fails closed on what it cannot observe at run time: an
actor-reading SQL function the planner may inline (it would leave no call in
the function statistics), dynamic SQL in a function body, opaque
non-extension languages, and SQL that binds an actor setting. A function
body that reads a setting by a non-literal name counts as actor-reading.
``python_accessors`` derives the ``apps.identity`` functions that reach the
actor from the census reference graph. ``ActorObserver`` records, for every
execution (every state, every deployed run; no sampling):
- exact call deltas of watched functions and touch deltas of actor relations
  from the per-oid transaction statistics (``track_functions = all``); a touch
  of an actor relation that no statement or called function explains fails
  closed;
- every statement any psycopg cursor sends (the driver is instrumented, so a
  raw connection is covered too), its parameters and its result rows, with the
  Python frames that sent it; COPY and streaming count as unobservable;
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
from uuid import UUID

import psycopg
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from psycopg import sql

from identity.permission_gate_census import CensusError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from types import CodeType, FrameType

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
    readers: SettingReaders
    # What the observer snapshots: actor relations by oid, and every function
    # that is actor-reading or reads an actor relation (only those can call
    # the actor or explain a touch).
    relation_oids: Mapping[str, int]
    watched: tuple[int, ...]


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
        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND n.nspname NOT LIKE 'pg\\_%%'"
    )
    functions = cursor.fetchall()
    readers = setting_readers(cursor)
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
        _grow_functions(functions, actor, actor_relations, settings, readers)
    ):
        pass
    _fail_closed(functions, actor, settings)
    cursor.execute(
        "SELECT c.relname, c.oid FROM pg_catalog.pg_class c "
        "WHERE c.relnamespace = 'clinic_app'::pg_catalog.regnamespace "
        "AND c.relname = ANY(%s)",
        [sorted(actor_relations)],
    )
    relation_oids = {str(name): int(oid) for name, oid in cursor.fetchall()}
    watched = tuple(
        sorted(
            set(actor) | {oid for oid, read in reads.items() if read & actor_relations}
        )
    )
    return ActorCatalog(
        settings,
        actor,
        frozenset(actor_relations),
        reads,
        names,
        frozenset(relations),
        readers,
        relation_oids,
        watched,
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
    readers: SettingReaders,
) -> bool:
    """Functions that read an actor setting (or a setting whose name is not a
    literal, or every setting), call an actor function, or read an actor
    relation where its policies apply (not a bypassing definer)."""
    before = len(actor)
    for oid, name, language, body, definer, _owner, bypass, *_rest in functions:
        if int(str(oid)) in actor or language not in ("sql", "plpgsql"):
            continue
        tokens = _tokens(str(body))
        sees_policies = not (definer and bypass)
        if (
            _reads_setting(str(body), settings)
            or any(
                reader != "set" and (not args or _named(args[0], settings))
                for reader, args in setting_calls(str(body), None, readers)
            )
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


@dataclass(frozen=True, slots=True)
class SettingReaders:
    """The builtins and catalog views that expose session settings.

    ``named`` take the setting name as their first argument; ``enumerators``
    return every setting at once, so any use of them is unresolvable.
    Derived from ``pg_proc`` (internal builtins whose C symbol handles a
    configuration setting by name or all settings) and the ``pg_catalog``
    views built on them.
    """

    named: frozenset[str]
    enumerators: frozenset[str]


# C symbols of the backend's setting accessors (show/set by name, show all).
_READER_SYMBOL: Final = re.compile(
    r"config_by_name|all_settings|settings_get_flags|all_file_settings"
)


def setting_readers(cursor: CursorWrapper) -> SettingReaders:
    """Derive every builtin and catalog view that reads a session setting."""
    cursor.execute(
        "SELECT p.proname, p.prosrc, COALESCE(p.proargtypes[0], 0) "
        "  = 'pg_catalog.text'::pg_catalog.regtype "
        "FROM pg_catalog.pg_proc p "
        "JOIN pg_catalog.pg_language l ON l.oid = p.prolang "
        "WHERE p.pronamespace = 'pg_catalog'::pg_catalog.regnamespace "
        "AND l.lanname IN ('internal', 'c')"
    )
    named: set[str] = set()
    enumerators: set[str] = set()
    for name, symbol, takes_name in cursor.fetchall():
        if _READER_SYMBOL.search(str(symbol)):
            (named if takes_name else enumerators).add(str(name))
    cursor.execute(
        "SELECT viewname, definition FROM pg_catalog.pg_views "
        "WHERE schemaname IN ('pg_catalog', 'information_schema')"
    )
    readers = named | enumerators
    for view, definition in cursor.fetchall():
        if _tokens(str(definition)) & readers:
            enumerators.add(str(view))
    return SettingReaders(frozenset(named), frozenset(enumerators))


_PLACEHOLDER: Final = re.compile(r"%(?:\((?P<key>[^)]+)\))?s")
_ARGUMENT: Final = re.compile(
    r"\s*(?:'(?P<literal>(?:[^']|'')*)'|(?P<placeholder>%(?:\([^)]+\))?s))"
    r"(?:\s*::\s*[a-z_ .]+)?\s*(?P<end>[,)])"
)
_SHOW: Final = re.compile(r"^\s*show\s+(?P<name>[a-z_][a-z0-9_.]*)", re.IGNORECASE)
_SET: Final = re.compile(
    r"^\s*set\s+(?:session\s+|local\s+)?(?P<name>[a-z_][a-z0-9_.]*)\s*(?:=|to)\s*"
    r"(?P<value>.*?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _parameter(sql_text: str, position: int, token: str, params: object) -> object:
    """The value a psycopg placeholder at ``position`` binds, or a sentinel."""
    key = _PLACEHOLDER.fullmatch(token)
    if key is not None and key.group("key") is not None:
        return (
            params.get(key.group("key"), _UNRESOLVED)
            if isinstance(params, dict)
            else _UNRESOLVED
        )
    index = len(_PLACEHOLDER.findall(sql_text[:position].replace("%%", "")))
    if isinstance(params, list | tuple) and index < len(params):
        return params[index]
    return _UNRESOLVED


_UNRESOLVED: Final = object()


def _arguments(sql_text: str, start: int, params: object, count: int) -> list[object]:
    """Resolve up to ``count`` leading arguments of a call opened at ``start``."""
    values: list[object] = []
    position = start
    for _ in range(count):
        match = _ARGUMENT.match(sql_text, position)
        if match is None:
            values.append(_UNRESOLVED)
            break
        if match.group("literal") is not None:
            values.append(match.group("literal").replace("''", "'"))
        else:
            values.append(
                _parameter(
                    sql_text,
                    match.start("placeholder"),
                    match.group("placeholder"),
                    params,
                )
            )
        position = match.end()
        if match.group("end") == ")":
            break
    return values


def setting_calls(
    sql_text: str, params: object, readers: SettingReaders
) -> list[tuple[str, list[object]]]:
    """Every setting-reader use in one statement, names resolved at run time.

    Each item is (reader, [name, value?]) with ``_UNRESOLVED`` wherever the
    name or value is neither a literal nor a bound parameter; enumerating
    readers (``pg_settings``, ``SHOW ALL``) resolve to nothing at all.
    """
    code = _COMMENT.sub(" ", sql_text)
    calls: list[tuple[str, list[object]]] = []
    for reader in sorted(readers.named):
        pattern = re.compile(rf'(?<![\w$])"?{re.escape(reader)}"?\s*\(', re.IGNORECASE)
        calls.extend(
            (reader, _arguments(code, match.end(), params, 2))
            for match in pattern.finditer(code)
        )
    lowered = _LITERAL.sub(" ", code.lower())
    calls.extend(
        (reader, [_UNRESOLVED])
        for reader in sorted(readers.enumerators)
        if re.search(rf'(?<![\w$])"?{re.escape(reader)}"?(?![\w$])', lowered)
    )
    show = _SHOW.match(code)
    if show is not None:
        name = show.group("name").lower()
        calls.append(("show", [_UNRESOLVED if name == "all" else name]))
    setting = _SET.match(code)
    if setting is not None:
        value = setting.group("value")
        placeholder = _PLACEHOLDER.fullmatch(value.strip())
        resolved: object = value.strip().strip("'")
        if placeholder is not None:
            resolved = _parameter(code, setting.start("value"), value.strip(), params)
        calls.append(("set", [setting.group("name").lower(), resolved]))
    return calls


def _named(value: object, settings: frozenset[str]) -> bool:
    """The resolved name is an actor setting, or it could not be resolved."""
    return value is _UNRESOLVED or (
        isinstance(value, str) and value.strip().lower() in settings
    )


@dataclass(slots=True)
class _Statement:
    """One sent statement; the sender's Python frames resolve only on demand."""

    sql: str
    params: object
    frame: FrameType | None
    carries: bool = False

    @property
    def stack(self) -> tuple[str, ...]:
        """Qualified names of the apps.* frames that sent it, innermost first."""
        names: list[str] = []
        for frame, _ in traceback.walk_stack(self.frame):
            module = frame.f_globals.get("__name__", "")
            if isinstance(module, str) and module.startswith("apps."):
                names.append(f"{module}.{frame.f_code.co_qualname}")
        return tuple(names)


_MAY_BIND: Final = re.compile(r"set_config|^\s*set\s", re.IGNORECASE)


@dataclass(slots=True)
class ActorObserver:
    """Collect actor observations for one execution at a time.

    Modes (``begin``):
    - ``bound``: the deployed staff context, an actor is bound. Every
      channel counts, including behaviour: the bound actor's id appearing in
      a statement's parameters, a statement's result rows or the outcome.
    - ``unbound``: a deployed patient or sessionless context, no actor
      bound. Every read then sees the same unbound value, so what counts is
      acquiring one: a statement binding an actor setting (name and value
      resolved at run time, unresolvable counts), or one bound at the end.
    - ``injected``: a matrix state that injects an actor into such a
      context. The injection is the test's, so only binding counts.
    Reads of a staff request's user and statement paths the observer cannot
    see (COPY, streaming) count in every mode.
    """

    catalog: ActorCatalog
    accessors: Mapping[CodeType, str]
    statements: list[_Statement] = field(default_factory=list)
    entered: list[str] = field(default_factory=list)
    blind: list[str] = field(default_factory=list)
    active: bool = False
    mode: str = "bound"
    values: frozenset[str] = frozenset()
    _raw: frozenset[bytes] = frozenset()
    _functions: dict[int, int] = field(default_factory=dict)
    _tables: dict[str, int] = field(default_factory=dict)
    # The last bound snapshot, reusable as the next execution's starting
    # point while the scope transaction is unchanged (``carry``/``drop``).
    _carried: tuple[dict[int, int], dict[str, int], list[str]] | None = None

    def _snapshot(self) -> tuple[dict[int, int], dict[str, int], list[str]]:
        """One statement: watched call counts, actor relation touches, the
        bound actor values (per-oid statistics, not the full views)."""
        self.active = False
        relations = sorted(self.catalog.relation_oids.items())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ARRAY(SELECT COALESCE("
                "  pg_catalog.pg_stat_get_xact_function_calls(f.oid), 0) "
                "  FROM pg_catalog.unnest(%s::oid[]) WITH ORDINALITY f(oid, n) "
                "  ORDER BY f.n), "
                "ARRAY(SELECT pg_catalog.pg_stat_get_xact_numscans(r.oid) "
                "  + pg_catalog.pg_stat_get_xact_tuples_inserted(r.oid) "
                "  + pg_catalog.pg_stat_get_xact_tuples_updated(r.oid) "
                "  + pg_catalog.pg_stat_get_xact_tuples_deleted(r.oid) "
                "  FROM pg_catalog.unnest(%s::oid[]) WITH ORDINALITY r(oid, n) "
                "  ORDER BY r.n), "
                "ARRAY(SELECT pg_catalog.current_setting(s, true) "
                "  FROM pg_catalog.unnest(%s::text[]) s), "
                "pg_catalog.current_setting('track_functions')",
                [
                    list(self.catalog.watched),
                    [oid for _, oid in relations],
                    sorted(self.catalog.settings),
                ],
            )
            row = cursor.fetchone()
        assert row is not None
        calls, touches, values, tracking = row
        assert tracking == "all", "track_functions must be all"
        return (
            dict(zip(self.catalog.watched, map(int, calls), strict=True)),
            {
                name: int(count)
                for (name, _), count in zip(relations, touches, strict=True)
            },
            [str(value) for value in values if value],
        )

    def begin(self, *, mode: str, actor: str | None = None) -> None:
        self.mode = mode
        if mode == "injected":
            assert actor, "an injected execution names its injected actor"
            self.values = frozenset({actor})
        if mode == "bound":
            snapshot, self._carried = self._carried or self._snapshot(), None
            self._functions, self._tables, values = snapshot
            assert values, "a bound execution needs a bound actor"
            self.values = frozenset(values)
            self._raw = frozenset(
                {value.encode() for value in values}
                | {_uuid_bytes(value) for value in values if _uuid_bytes(value)}
            )
        elif mode == "unbound":
            assert not self._bound_values(), "the deployed context binds an actor"
            self.values = frozenset()
        self.statements.clear()
        self.entered.clear()
        self.blind.clear()
        _READS.clear()
        self.active = True

    def _bound_values(self) -> list[str]:
        self.active = False
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ARRAY(SELECT pg_catalog.current_setting(s, true) "
                "FROM pg_catalog.unnest(%s::text[]) s)",
                [sorted(self.catalog.settings)],
            )
            row = cursor.fetchone()
        return [str(value) for value in (row[0] if row else []) if value]

    def end(self, outcome: object = None) -> list[str]:
        self.active = False
        found: list[str] = [
            f"binds an actor setting from {statement.stack[:1]}"
            for statement in self.statements
            if self._binds(statement)
        ]
        if self.mode == "bound":
            found.extend(self._bound_findings())
            if outcome is not None and any(
                value in repr(outcome) for value in self.values
            ):
                found.append("returns the actor id")
        elif self.mode == "unbound" and self._bound_values():
            found.append("leaves an actor bound")
        found.extend(self.blind)
        found.extend(sorted(set(_READS)))
        _READS.clear()
        return sorted(set(found))

    def _binds(self, statement: _Statement) -> bool:
        """A statement acquires an actor: it binds an actor setting (or one
        it cannot resolve) to a value other than empty or the actor already
        bound (a lifecycle helper restoring the saved value acquires none)."""
        if not _MAY_BIND.search(statement.sql):
            return False
        settings = self.catalog.settings
        for reader, arguments in setting_calls(
            statement.sql, statement.params, self.catalog.readers
        ):
            if (
                reader in ("set_config", "set")
                and arguments
                and _named(arguments[0], settings)
            ):
                value = arguments[1] if len(arguments) > 1 else _UNRESOLVED
                if value is _UNRESOLVED or (
                    value not in (None, "") and str(value) not in self.values
                ):
                    return True
        return False

    def drop_carried(self) -> None:
        """A new scope transaction starts: statistics restart from zero."""
        self._carried = None

    def _bound_findings(self) -> list[str]:
        snapshot = self._snapshot()
        # Nothing between two inputs of one scope moves the statistics (the
        # input's savepoint release or rollback does not), so this end is
        # the next input's start.
        self._carried = snapshot
        functions, tables, _ = snapshot
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
        for statement in self.statements:
            explained |= _tokens(statement.sql) & self.catalog.relation_names
            found.extend(self._statement_findings(statement))
        touched = {
            name for name, count in tables.items() if count > self._tables.get(name, 0)
        }
        found.extend(
            f"unexplained touch of actor relation {name}"
            for name in sorted((touched & self.catalog.relations) - explained)
        )
        found.extend(f"enters {name}" for name in self.entered)
        return found

    def _statement_findings(self, statement: _Statement) -> list[str]:
        settings = self.catalog.settings
        tokens = _tokens(statement.sql)
        actor_functions = set(self.catalog.functions.values())
        found: list[str] = []
        reads = [
            reader
            for reader, arguments in setting_calls(
                statement.sql, statement.params, self.catalog.readers
            )
            if reader != "set" and (not arguments or _named(arguments[0], settings))
        ]
        found.extend(
            f"statement reads an actor or unresolvable setting via {reader} "
            f"from {statement.stack[:1]}"
            for reader in reads
        )
        if any(value in repr(statement.params) for value in self.values):
            found.append(f"statement sends the actor id from {statement.stack[:1]}")
        if statement.carries:
            found.append(f"statement receives the actor id from {statement.stack[:1]}")
        found.extend(
            f"statement calls clinic_app.{name}"
            for name in sorted(tokens & actor_functions)
        )
        found.extend(
            f"statement touches actor relation {relation}"
            for relation in sorted(tokens & self.catalog.relations)
        )
        sender = next(
            (name for name in statement.stack if name.startswith("apps.identity.")), ""
        )
        if (
            sender
            and (reads or statement.carries or tokens & actor_functions)
            and sender not in set(self.accessors.values())
        ):
            found.append(f"unlisted accessor {sender} reaches the actor")
        return found

    def record(self, sql_text: str, params: object, result: object) -> None:
        """One statement a cursor executed while observing."""
        carries = self.mode == "bound" and _carries(result, self._raw)
        frame = sys._getframe(2)  # the sender, resolved on demand
        self.statements.append(_Statement(sql_text, params, frame, carries))

    def on_start(self, code: CodeType) -> None:
        if self.active and code in self.accessors:
            self.entered.append(self.accessors[code])

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Harness setup inside a probe input (not the probed function)."""
        active, self.active = self.active, False
        try:
            yield
        finally:
            self.active = active


def _uuid_bytes(value: str) -> bytes:
    try:
        return UUID(value).bytes
    except ValueError:
        return b""


def _carries(result: object, raw: frozenset[bytes]) -> bool:
    """Whether any value of a psycopg result holds one of ``raw`` (text or
    binary form)."""
    if result is None or not raw:
        return False
    rows = getattr(result, "ntuples", 0)
    columns = getattr(result, "nfields", 0)
    for row in range(rows):
        for column in range(columns):
            value = result.get_value(row, column)  # type: ignore[attr-defined]
            if value and any(needle in bytes(value) for needle in raw):
                return True
    return False


def _text(query: object, cursor: object) -> str:
    if isinstance(query, str):
        return query
    if isinstance(query, bytes):
        return query.decode()
    if isinstance(query, sql.Composable):
        return query.as_string(cursor)  # type: ignore[arg-type]
    return str(query)


def _observed_methods(
    observer: ActorObserver, originals: Mapping[str, Callable[..., object]]
) -> dict[str, Callable[..., object]]:
    """Cursor methods that record what they send while the observer is active."""
    execute, executemany = originals["execute"], originals["executemany"]

    def observed_execute(
        self: psycopg.Cursor[object],
        query: object,
        params: object = None,
        **kwargs: object,
    ) -> object:
        result = execute(self, query, params, **kwargs)
        if observer.active:
            observer.record(_text(query, self), params, self.pgresult)
        return result

    def observed_executemany(
        self: psycopg.Cursor[object],
        query: object,
        params_seq: object,
        **kwargs: object,
    ) -> object:
        rows = list(params_seq)  # type: ignore[call-overload]
        result = executemany(self, query, rows, **kwargs)
        if observer.active:
            for params in rows:
                observer.record(_text(query, self), params, None)
        return result

    def blind(name: str, what: str) -> Callable[..., object]:
        method = originals[name]

        def run(self: object, *args: object, **kwargs: object) -> object:
            if observer.active:
                observer.blind.append(f"uses {what}, which the observer cannot inspect")
            return method(self, *args, **kwargs)

        return run

    return {
        "execute": observed_execute,
        "executemany": observed_executemany,
        "stream": blind("stream", "a streaming cursor"),
        "copy": blind("copy", "COPY"),
    }


@contextmanager
def statements_captured(observer: ActorObserver) -> Iterator[None]:
    """Record every statement any psycopg cursor sends while observing.

    Instrumenting the driver (not Django's execute wrapper) also covers raw
    connections and cursors; COPY and streaming, whose rows the observer
    cannot inspect, count as observations while it is active.
    """
    cursor_class = psycopg.Cursor
    originals: dict[str, Callable[..., object]] = {
        name: getattr(cursor_class, name)
        for name in ("execute", "executemany", "stream", "copy")
    }
    for name, method in _observed_methods(observer, originals).items():
        setattr(cursor_class, name, method)
    try:
        yield
    finally:
        for name, method in originals.items():
            setattr(cursor_class, name, method)
