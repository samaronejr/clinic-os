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

- a session-setting read, whether the statement comes from Python or from the
  database. The reader set is derived: the backend's setting builtins by oid
  (``setting_readers``), the views built on them, and every operator whose
  function is one of them. Text (client statements, function bodies,
  prepared statements) is classified against that set, a stored expression
  by oid. Every use has its name resolved: in a client statement at run time
  from literals and bound parameters, in a stored expression from its
  constant argument. A name that resolves to an actor setting, a name that
  cannot be resolved (computed, a column, a variable, an operator operand),
  and every enumerating reader (``pg_show_all_settings`` and whatever is
  built on it, such as ``pg_settings``, and ``SHOW ALL``) count. Spelling
  does not matter; text whose meaning cannot be read at all (SQL run from
  text by a derived executor such as ``query_to_xml``, a DO block running
  dynamic SQL, a Unicode-escaped identifier) counts wherever reading or
  binding counts;
- a database object whose evaluation reaches one. ``actor_catalog`` derives
  them by oid over every schema: function bodies (``prosrc``) and every
  expression the catalog stores, which is every ``pg_node_tree`` column of
  ``pg_catalog`` (column defaults and generated columns, CHECK and domain
  constraints, domain defaults, index expressions and predicates, partition
  keys and bounds, view and rule actions, row policies, trigger conditions,
  SQL-standard function bodies and argument defaults, and whatever a new
  PostgreSQL adds). That column list is read from the live catalog and must
  equal ``EXPRESSION_COLUMNS``, which maps each column to the object whose use
  evaluates it, so a new expression column fails closed instead of going
  unscanned. A tree's references are harvested from every field (oids are
  unique across catalogs), and evaluation follows operators, types, casts,
  aggregates, column types and domain bases (the referencing columns come
  from ``pg_get_catalog_foreign_keys``) to a fixed point. The objects found
  are functions (whose calls are counted), relations (touching one evaluates
  its policies, defaults, constraints, rules, conditions; a policy is not
  seen by a definer that bypasses row security) and types;
- a database object that can bind an actor setting (``binders``: the same
  derivation, seeded by ``set_config`` on an actor or unresolvable name, and
  following triggers too), which counts wherever binding counts;
- session state a statement reaches without naming what it does: prepared
  statements and the session's temporary schema are read for every
  execution, a prepared statement's text classified like any statement, and
  a temporary object the catalog was not derived with makes it derive again
  (one it cannot observe fails closed);
- the request's authenticated user, for code handed a staff request.

What text classification cannot fully see is bounded by enforcement, not by
parser completeness: ``test_certifier_boundary`` refuses those constructs
anywhere in ``apps/`` (operators over readers, server-side PREPARE, pg_temp
objects, unresolvable setting names, SQL run from text, dynamic EXECUTE,
Unicode escapes, libpq reached directly, text the lexer cannot settle).
Every text is read through one lexer (``identity/sql_lexer.py``, shared with
that guard): quoted forms are settled left to right before comments, and
what it cannot settle is unreadable, never guessed.

Behaviour backs the reading rules: with an actor bound, the actor's id showing
up in any statement's text or parameters, in anything the server sends back,
or in the outcome counts as an observation however it was obtained.

The catalog fails closed on what it cannot observe at run time: an
actor-reading SQL function the planner may inline (it would leave no call in
the function statistics), dynamic SQL in a function body, opaque non-extension
languages, a function that binds an actor setting (in its body or its SET
clause), and a stored expression it cannot read. ``python_accessors`` derives
the ``apps.identity`` functions that reach the actor from the census reference
graph. ``ActorObserver`` records, for every execution (every state, every
deployed run; no sampling):
- exact call deltas of watched functions and touch deltas of actor relations
  from the per-oid transaction statistics (``track_functions = all``); a touch
  of an actor relation that no statement or called function explains fails
  closed;
- every statement psycopg sends, at the send calls every cursor class
  (client, server-side, raw), pipeline and COPY goes through, with its
  parameters and the Python frames that sent it;
- libpq's own protocol trace of the probed connection (``PQtrace``): whatever
  reaches the server on that connection is a message libpq composed and
  traced, so every execution message (Query, Execute, FunctionCall) must match
  a recorded send, or the execution fails closed. Nothing on the connection
  can bypass the recording without the counts disagreeing. The trace is also
  searched for the actor id in both directions, and COPY counts as
  unobservable;
- entry into an accessor (``sys.monitoring``), with an accessor outside the
  derived set failing closed;
- reads of a staff request's ``user``.
"""

from __future__ import annotations

import ast
import bisect
import ctypes
import dataclasses
import os
import re
import sys
import tempfile
import traceback
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

import psycopg
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from psycopg import pq, sql

from identity.permission_gate_census import CensusError
from identity.sql_lexer import lex

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
    from types import CodeType, FrameType

    from django.db.backends.utils import CursorWrapper
    from django.http import HttpRequest
    from psycopg.pq.abc import PGconn

    from identity.permission_gate_census import Graph

_TOKEN: Final = re.compile(r"[a-z_][a-z0-9_$]*")
_DYNAMIC: Final = re.compile(r"\bexecute\b")
# With it off, a backslash escapes a quote in a plain literal: every text
# boundary the lexer settled could move (identity/sql_lexer.py).
_STANDARD_STRINGS: Final = "standard_conforming_strings"
# The runtime role and the owner role (owner-only paths run as it).
RUNTIME_ROLES: Final = ("clinic_app", "clinic_owner")
_SYSTEM_SCHEMAS: Final = ("pg_catalog", "information_schema")
# What the observer reads around an execution: watched call counts, actor
# relation touches, the bound actor values, and the session state (temporary
# schema, prepared statements, standard_conforming_strings).
type _Snapshot = tuple[dict[int, int], dict[int, int], list[str], int, int, str]


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
    # Actor-observing functions by oid (bare name; schema-qualified below).
    functions: Mapping[int, str]
    relations: frozenset[str]
    reads: Mapping[int, frozenset[str]]
    names: Mapping[int, str]
    relation_names: frozenset[str]
    readers: SettingReaders
    # What the observer snapshots: actor relations by oid, and every function
    # that is actor-reading or reads an actor relation (only those can call
    # the actor or explain a touch).
    relation_oids: tuple[tuple[int, str], ...]
    watched: tuple[int, ...]
    qualified: Mapping[int, str] = field(default_factory=dict)
    # Types whose evaluation observes the actor (domain constraints and
    # defaults, input or cast functions), by name.
    types: frozenset[str] = frozenset()
    # Everything whose evaluation can bind an actor setting: function,
    # relation and type names, and operator symbols.
    binders: frozenset[str] = frozenset()
    # Symbols of operators whose function observes the actor.
    operators: frozenset[str] = frozenset()
    # The session's temporary objects the catalog was derived with.
    temporary: frozenset[int] = frozenset()


def _code(text: str) -> str:
    """The text with its comments blanked, lowered (``sql_lexer`` ``code``)."""
    return lex(text).code.lower()


def _bare(text: str) -> str:
    """The text with its comments and string literals blanked, lowered."""
    return lex(text).bare.lower()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(_bare(text)))


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


def _system(schema: str) -> bool:
    """Schemas PostgreSQL ships. A session's temporary schema holds objects
    the session created and is scanned like any other."""
    if schema.startswith(("pg_temp_", "pg_toast_temp_")):
        return False
    return schema in _SYSTEM_SCHEMAS or schema.startswith("pg_")


# The expression columns of the system catalog, each mapped to the objects
# whose use evaluates its trees: (kind, catalog column holding their oid).
# ``relation``: touching the relation; ``policy``: touching it as a role the
# policy applies to; ``type``: coercing to or producing the type;
# ``function``: calling it. ``node_tree_columns`` reads the list from the live
# catalog and ``actor_catalog`` refuses to run unless the two are equal.
EXPRESSION_COLUMNS: Final[Mapping[tuple[str, str], tuple[tuple[str, str], ...]]] = {
    ("pg_attrdef", "adbin"): (("relation", "adrelid"),),
    ("pg_class", "relpartbound"): (("relation", "oid"),),
    ("pg_constraint", "conbin"): (("relation", "conrelid"), ("type", "contypid")),
    ("pg_index", "indexprs"): (("relation", "indrelid"),),
    ("pg_index", "indpred"): (("relation", "indrelid"),),
    ("pg_partitioned_table", "partexprs"): (("relation", "partrelid"),),
    ("pg_policy", "polqual"): (("policy", "polrelid"),),
    ("pg_policy", "polwithcheck"): (("policy", "polrelid"),),
    ("pg_proc", "proargdefaults"): (("function", "oid"),),
    ("pg_proc", "prosqlbody"): (("function", "oid"),),
    ("pg_publication_rel", "prqual"): (("relation", "prrelid"),),
    ("pg_rewrite", "ev_action"): (("relation", "ev_class"),),
    ("pg_rewrite", "ev_qual"): (("relation", "ev_class"),),
    ("pg_statistic_ext", "stxexprs"): (("relation", "stxrelid"),),
    ("pg_trigger", "tgqual"): (("relation", "tgrelid"),),
    ("pg_type", "typdefaultbin"): (("type", "oid"),),
}

# Catalogs whose rows run functions when their owner is used, with the owner
# column. Which of their columns name functions is derived
# (``pg_get_catalog_foreign_keys``); triggers only matter for binding (their
# calls are counted wherever reading counts).
_INVOKED_THROUGH: Final[Mapping[str, tuple[str, str]]] = {
    "pg_aggregate": ("evaluates", "aggfnoid"),
    "pg_cast": ("evaluates", "casttarget"),
    "pg_operator": ("evaluates", "oid"),
    "pg_range": ("evaluates", "rngtypid"),
    "pg_trigger": ("triggers", "tgrelid"),
    "pg_type": ("evaluates", "oid"),
}


def node_tree_columns(cursor: CursorWrapper) -> frozenset[tuple[str, str]]:
    """Every expression column of the system catalog (type ``pg_node_tree``)."""
    cursor.execute(
        "SELECT c.relname, a.attname FROM pg_catalog.pg_attribute a "
        "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = 'pg_catalog'::pg_catalog.regnamespace "
        "AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped "
        "AND a.atttypid = 'pg_catalog.pg_node_tree'::pg_catalog.regtype"
    )
    return frozenset((str(table), str(column)) for table, column in cursor.fetchall())


@dataclass(frozen=True, slots=True)
class _Function:
    oid: int
    schema: str
    name: str
    language: str
    body: str
    definer: bool
    bypass: bool
    config: tuple[str, ...]
    extension: bool

    @property
    def system(self) -> bool:
        return _system(self.schema)

    @property
    def blind(self) -> bool:
        """A definer owned by a role that bypasses row security: the policies
        of what it reads never run."""
        return self.definer and self.bypass


@dataclass(slots=True)
class _Graph:
    """What evaluating each object (by oid) evaluates in turn, and what reads
    or binds an actor setting itself."""

    evaluates: dict[int, set[int]] = field(default_factory=dict)
    policies: dict[int, set[int]] = field(default_factory=dict)
    triggers: dict[int, set[int]] = field(default_factory=dict)
    reads: set[int] = field(default_factory=set)
    policy_reads: set[int] = field(default_factory=set)
    binds: set[int] = field(default_factory=set)
    tokens: dict[int, set[str]] = field(default_factory=dict)
    code: dict[int, str] = field(default_factory=dict)


def actor_catalog(cursor: CursorWrapper, settings: frozenset[str]) -> ActorCatalog:
    """Derive actor-observing functions, relations and types to a fixed point."""
    assert settings, "no setting carries the actor"
    builtins = setting_readers(cursor)
    functions = _functions(cursor)
    relations = _objects(
        cursor,
        "SELECT c.oid, n.nspname, c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')",
    )
    types = _objects(
        cursor,
        "SELECT t.oid, n.nspname, t.typname FROM pg_catalog.pg_type t "
        "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace",
    )
    operators = _objects(
        cursor,
        "SELECT o.oid, n.nspname, o.oprname FROM pg_catalog.pg_operator o "
        "JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace",
    )
    graph = _Graph()
    _add_invocations(cursor, graph)
    reader_operators = {
        oid
        for oid in operators
        if graph.evaluates.get(oid, set())
        & (set(builtins.named_oids) | builtins.enumerator_oids | builtins.executor_oids)
    }
    # The derived reader set, as text sees it: operators whose function is a
    # reader are readers (their operand cannot be resolved).
    builtins = dataclasses.replace(
        builtins,
        operators=frozenset(operators[oid][1] for oid in reader_operators),
    )
    _add_trees(cursor, graph, builtins, settings, reader_operators)
    _add_column_types(cursor, graph)
    _add_bodies(graph, functions, builtins, settings)
    actor, _ = _actor_closure(graph, functions, relations, types, operators)
    binders = _binder_closure(graph, functions, relations, operators)
    _fail_closed(
        functions.values(),
        actor,
        settings,
        frozenset(
            oid
            for oid, function in functions.items()
            if function.language in ("sql", "plpgsql")
            and _executor_calls(function.body, builtins)
        ),
    )
    names = {oid: function.name for oid, function in functions.items()}
    relation_names = frozenset(name for _, name in relations.values())
    reads: dict[int, frozenset[str]] = {}
    for oid, tokens in graph.tokens.items():
        reads[oid] = frozenset(tokens & relation_names)
    for oid in functions:
        referenced = {
            relations[t][1] for t in graph.evaluates.get(oid, ()) if t in relations
        }
        if referenced:
            reads[oid] = reads.get(oid, frozenset()) | referenced
    actor_functions = {oid: functions[oid] for oid in sorted(actor) if oid in functions}
    actor_relations = {oid: relations[oid] for oid in sorted(actor) if oid in relations}
    relation_set = {name for _, name in actor_relations.values()}
    watched = tuple(
        sorted(
            set(actor_functions)
            | {oid for oid, read in reads.items() if read & relation_set}
        )
    )
    # Catalog views over the setting builtins read settings like the builtins.
    readers = dataclasses.replace(
        builtins,
        enumerators=builtins.enumerators
        | {name for schema, name in actor_relations.values() if _system(schema)},
    )
    binder_names = {
        objects[oid][1]
        for objects in (_function_names(functions), relations, types, operators)
        for oid in binders
        if oid in objects
    }
    cursor.execute(_TEMPORARY_OBJECTS)
    temporary = frozenset(int(oid) for (oid,) in cursor.fetchall())
    return ActorCatalog(
        settings=settings,
        functions={oid: function.name for oid, function in actor_functions.items()},
        relations=frozenset(relation_set),
        reads=reads,
        names=names,
        relation_names=relation_names,
        readers=readers,
        relation_oids=tuple((oid, name) for oid, (_, name) in actor_relations.items()),
        watched=watched,
        qualified={
            oid: f"{function.schema}.{function.name}"
            for oid, function in actor_functions.items()
        },
        types=frozenset(
            name
            for oid, (schema, name) in types.items()
            if oid in actor and not _system(schema)
        ),
        binders=frozenset(binder_names),
        operators=frozenset(
            symbol for oid, (_, symbol) in operators.items() if oid in actor
        ),
        temporary=temporary,
    )


# The objects in this session's temporary schema (every object records a
# dependency on its schema).
_TEMPORARY_OBJECTS: Final = (
    "SELECT objid FROM pg_catalog.pg_depend "
    "WHERE refclassid = 'pg_catalog.pg_namespace'::pg_catalog.regclass "
    "AND refobjid = pg_catalog.pg_my_temp_schema()"
)


def _function_names(functions: Mapping[int, _Function]) -> dict[int, tuple[str, str]]:
    return {
        oid: (function.schema, function.name) for oid, function in functions.items()
    }


def _functions(cursor: CursorWrapper) -> dict[int, _Function]:
    cursor.execute(
        "SELECT p.oid, n.nspname, p.proname, l.lanname, p.prosrc, p.prosecdef, "
        "r.rolbypassrls, COALESCE(p.proconfig, '{}'::pg_catalog.text[]), "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
        "  WHERE d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass "
        "  AND d.objid = p.oid AND d.deptype = 'e') "
        "FROM pg_catalog.pg_proc p "
        "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_catalog.pg_language l ON l.oid = p.prolang "
        "JOIN pg_catalog.pg_roles r ON r.oid = p.proowner"
    )
    return {
        int(oid): _Function(
            int(oid),
            str(schema),
            str(name),
            str(language),
            str(body or ""),
            bool(definer),
            bool(bypass),
            tuple(str(entry) for entry in config),
            bool(extension),
        )
        for oid, schema, name, language, body, definer, bypass, config, extension in (
            cursor.fetchall()
        )
    }


def _objects(cursor: CursorWrapper, query: str) -> dict[int, tuple[str, str]]:
    cursor.execute(query)
    return {
        int(oid): (str(schema), str(name)) for oid, schema, name in cursor.fetchall()
    }


def _add_invocations(cursor: CursorWrapper, graph: _Graph) -> None:
    """Functions an object runs when used: an operator its code and
    estimators, a type its I/O, typmod and subscript functions (and casts to
    it), an aggregate its support functions, a range type its canonical and
    difference functions, a relation its triggers."""
    cursor.execute(
        "SELECT fktable::pg_catalog.text, fkcols "
        "FROM pg_catalog.pg_get_catalog_foreign_keys() "
        "WHERE pktable = 'pg_catalog.pg_proc'::pg_catalog.regclass AND NOT is_array"
    )
    columns: dict[str, set[str]] = {}
    for table, keys in cursor.fetchall():
        name = str(table).removeprefix("pg_catalog.")
        if name in _INVOKED_THROUGH:
            columns.setdefault(name, set()).update(str(key) for key in keys)
    for catalog, (kind, owner) in sorted(_INVOKED_THROUGH.items()):
        called = sorted(columns.get(catalog, set()) - {owner})
        assert called, f"{catalog} names no function"
        query = sql.SQL(
            "SELECT {owner}::pg_catalog.oid, {called} FROM pg_catalog.{table}"
        ).format(
            owner=sql.Identifier(owner),
            called=sql.SQL(", ").join(
                sql.SQL("{}::pg_catalog.oid").format(sql.Identifier(column))
                for column in called
            ),
            table=sql.Identifier(catalog),
        )
        cursor.execute(_rendered(query, cursor))
        target = graph.triggers if kind == "triggers" else graph.evaluates
        for owner_oid, *calls in cursor.fetchall():
            target.setdefault(int(owner_oid), set()).update(
                int(call) for call in calls if call
            )


def _rendered(query: sql.Composable, cursor: CursorWrapper) -> str:
    return query.as_string(cast("Any", cursor).cursor)


def _add_column_types(cursor: CursorWrapper, graph: _Graph) -> None:
    """A relation evaluates its column types (domain constraints and defaults
    run on writes); a domain its base type, an array its element type."""
    cursor.execute(
        "SELECT a.attrelid, a.atttypid FROM pg_catalog.pg_attribute a "
        "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE a.attnum > 0 AND NOT a.attisdropped "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND n.nspname NOT LIKE 'pg\\_%%'"
    )
    for relation, column_type in cursor.fetchall():
        graph.evaluates.setdefault(int(relation), set()).add(int(column_type))
    cursor.execute(
        "SELECT oid, typbasetype, typelem FROM pg_catalog.pg_type "
        "WHERE typbasetype <> 0 OR typelem <> 0"
    )
    for type_oid, base, element in cursor.fetchall():
        graph.evaluates.setdefault(int(type_oid), set()).update(
            int(other) for other in (base, element) if other
        )


def _add_trees(
    cursor: CursorWrapper,
    graph: _Graph,
    readers: SettingReaders,
    settings: frozenset[str],
    reader_operators: set[int],
) -> None:
    """Every stored expression of every schema, attached to its owner."""
    for kind, catalog, column, owner, applies, tree in _expression_trees(cursor):
        if kind == "policy" and not applies:
            continue
        where = f"{catalog}.{column} of oid {owner}"
        fields = _reference_fields(tree, where)
        references = {oid for _, oid in fields}
        reads, binds = _tree_setting_uses(
            tree, fields, readers, settings, reader_operators, where
        )
        edges = graph.policies if kind == "policy" else graph.evaluates
        edges.setdefault(owner, set()).update(references)
        if reads:
            (graph.policy_reads if kind == "policy" else graph.reads).add(owner)
        if binds:
            graph.binds.add(owner)


def _expression_trees(
    cursor: CursorWrapper,
) -> list[tuple[str, str, str, int, bool, str]]:
    """(owner kind, catalog, column, owner oid, applies, tree) for every
    stored expression; the mapped columns must be exactly the live ones."""
    live = node_tree_columns(cursor)
    mapped = frozenset(EXPRESSION_COLUMNS)
    if live != mapped:
        message = (
            "pg_catalog expression columns and the census disagree: "
            f"unmapped {sorted(live - mapped)}, gone {sorted(mapped - live)}"
        )
        raise CensusError(message)
    roles = sql.SQL(", ").join(sql.Literal(role) for role in RUNTIME_ROLES)
    parts: list[sql.Composable] = []
    for (catalog, column), owners in sorted(EXPRESSION_COLUMNS.items()):
        for kind, owner in owners:
            applies: sql.Composable = sql.SQL("true")
            if kind == "policy":
                applies = sql.SQL(
                    "EXISTS (SELECT 1 FROM pg_catalog.unnest(polroles) r "
                    "WHERE r = 0 OR pg_catalog.pg_get_userbyid(r) IN ({}))"
                ).format(roles)
            parts.append(
                sql.SQL(
                    "SELECT {kind}, {catalog}, {column}, {owner}::pg_catalog.oid, "
                    "{applies}, {tree}::pg_catalog.text FROM pg_catalog.{table} "
                    "WHERE {tree} IS NOT NULL AND {owner} <> 0"
                ).format(
                    kind=sql.Literal(kind),
                    catalog=sql.Literal(catalog),
                    column=sql.Literal(column),
                    owner=sql.Identifier(owner),
                    applies=applies,
                    tree=sql.Identifier(column),
                    table=sql.Identifier(catalog),
                )
            )
    cursor.execute(_rendered(sql.SQL(" UNION ALL ").join(parts), cursor))
    return [
        (str(kind), str(catalog), str(column), int(owner), bool(applies), str(tree))
        for kind, catalog, column, owner, applies, tree in cursor.fetchall()
    ]


# A node field and its integer value, or an oid list: every reference a
# stored tree can hold, whatever the node type. Positions are not references.
_REFERENCE: Final = re.compile(r":([A-Za-z_]+) (?:(\d+)|\(o((?: \d+)+)\))")
_NOT_REFERENCES: Final = frozenset({"location", "stmt_location", "stmt_len"})
_FUNCTION_FIELD: Final = re.compile("func|fn", re.IGNORECASE)
_OPERATOR_FIELD: Final = re.compile("op", re.IGNORECASE)
_NODE_TOKEN: Final = re.compile(r"[{}()]|(?:[^\s{}()\\]|\\.)+")
_BRACE: Final = re.compile(r"(?<!\\)[{}()]")


def _reference_fields(tree: str, where: str) -> list[tuple[str, int]]:
    """(field, oid) for every integer field and oid list of a stored tree.

    A tree that is not balanced node syntax cannot be read and fails closed;
    ``<>`` is the empty node (no expression).
    """
    if tree == "<>":
        return []
    depth = {"{": 0, "(": 0}
    for brace in _BRACE.findall(tree):
        if brace in "{(":
            depth[brace] += 1
        else:
            depth["{" if brace == "}" else "("] -= 1
    if any(depth.values()) or not tree.startswith(("{", "(")):
        message = f"{where} cannot be read as a stored expression"
        raise CensusError(message)
    found: list[tuple[str, int]] = []
    for name, value, listed in _REFERENCE.findall(tree):
        if listed:
            found.extend((name, int(item)) for item in listed.split())
        elif name not in _NOT_REFERENCES:
            found.append((name, int(value)))
    return found


def _tree_setting_uses(  # noqa: PLR0913 - one tree needs the reader context
    tree: str,
    fields: list[tuple[str, int]],
    readers: SettingReaders,
    settings: frozenset[str],
    reader_operators: set[int],
    where: str,
) -> tuple[bool, bool]:
    """(reads, binds): the tree reads or binds an actor setting, one it
    cannot resolve, or every setting."""
    runs = any(
        oid in readers.executor_oids and _FUNCTION_FIELD.search(name)
        for name, oid in fields
    )
    reads = runs or any(
        (oid in readers.enumerator_oids and _FUNCTION_FIELD.search(name))
        or (oid in reader_operators and _OPERATOR_FIELD.search(name))
        for name, oid in fields
    )
    binds = runs
    if any(oid in readers.named_oids for _, oid in fields):
        for oid, argument in _named_calls(tree, readers.named_oids, where):
            if _named(argument, settings):
                reads = True
                binds = binds or oid in readers.setter_oids
    return reads, binds


def _named_calls(
    tree: str, named: Mapping[int, str], where: str
) -> list[tuple[int, object]]:
    """(reader oid, first argument) for every reference to a named reader in
    a tree; anything but a direct call with a constant name is unresolved."""
    tokens = _NODE_TOKEN.findall(tree)
    calls: list[tuple[int, object]] = []
    for index, piece in enumerate(tokens):
        if not piece.isdigit() or int(piece) not in named or index == 0:
            continue
        if not _FUNCTION_FIELD.search(tokens[index - 1]):
            continue  # an integer field that happens to equal the oid
        argument: object = _UNRESOLVED
        if tokens[index - 1] == ":funcid" and tokens[index - 3 : index - 1] == [
            "{",
            "FUNCEXPR",
        ]:
            node = _parsed(tokens, index - 3, where)
            arguments = node.get("args") if isinstance(node, dict) else None
            if isinstance(arguments, list) and arguments:
                argument = _constant_text(arguments[0])
        calls.append((int(piece), argument))
    return calls


def _parsed(tokens: list[str], index: int, where: str) -> object:
    try:
        node, _ = _node(tokens, index)
    except (IndexError, ValueError) as error:
        message = f"{where} cannot be read as a stored expression"
        raise CensusError(message) from error
    return node


def _node(tokens: list[str], index: int) -> tuple[object, int]:
    """One value of node syntax: a node, a list, or an atom."""
    piece = tokens[index]
    if piece == "{":
        node: dict[str, object] = {"": tokens[index + 1]}
        index += 2
        while tokens[index] != "}":
            name = tokens[index]
            if not name.startswith(":"):
                raise ValueError(name)
            value, index = _node(tokens, index + 1)
            if index < len(tokens) and tokens[index] == "[":
                end = tokens.index("]", index)
                value = ("datum", [int(item) for item in tokens[index + 1 : end]])
                index = end + 1
            node[name[1:]] = value
        return node, index + 1
    if piece == "(":
        items: list[object] = []
        index += 1
        while tokens[index] != ")":
            item, index = _node(tokens, index)
            items.append(item)
        return items, index + 1
    return piece, index + 1


def _constant_text(node: object) -> object:
    """The text of a constant (through a relabeling), or a sentinel."""
    while isinstance(node, dict) and node.get("") == "RELABELTYPE":
        node = node.get("arg")
    if not isinstance(node, dict) or node.get("") != "CONST":
        return _UNRESOLVED
    if node.get("constisnull") == "true":
        return None
    datum = node.get("constvalue")
    length = node.get("constlen")
    if not (isinstance(datum, tuple) and isinstance(length, str)):
        return _UNRESOLVED
    return _datum_text(bytes(datum[1]), int(length))


def _datum_text(raw: bytes, length: int) -> object:
    """Decode a stored text datum (varlena, cstring or name), or a sentinel.

    Anything else (external or compressed, a size that does not match) is
    unresolved rather than guessed.
    """
    try:
        if length == -1:
            if not raw or raw[0] == 0x01 or (raw[0] & 0x03) == 0x02:
                return _UNRESOLVED
            if raw[0] & 0x01:
                size, header = raw[0] >> 1, 1
            else:
                size, header = int.from_bytes(raw[:4], "little") >> 2, 4
            if size != len(raw):
                return _UNRESOLVED
            return raw[header:].decode()
        if length == -2 or length > 0:
            return raw.split(b"\0", 1)[0].decode()
    except UnicodeDecodeError:
        return _UNRESOLVED
    return _UNRESOLVED


def _add_bodies(
    graph: _Graph,
    functions: Mapping[int, _Function],
    readers: SettingReaders,
    settings: frozenset[str],
) -> None:
    """Function bodies written as text: what they name, read and bind."""
    for function in functions.values():
        if function.language not in ("sql", "plpgsql"):
            continue
        body = function.body
        graph.tokens[function.oid] = _tokens(body)
        graph.code[function.oid] = _bare(body)
        calls = setting_calls(body, None, readers)
        if _executor_calls(body, readers):
            graph.reads.add(function.oid)
            graph.binds.add(function.oid)
        if _reads_setting(body, settings) or any(
            reader != "set" and (not arguments or _named(arguments[0], settings))
            for reader, arguments in calls
        ):
            graph.reads.add(function.oid)
        if any(
            reader in ("set_config", "set")
            and arguments
            and _named(arguments[0], settings)
            for reader, arguments in calls
        ):
            graph.binds.add(function.oid)


def _actor_closure(
    graph: _Graph,
    functions: Mapping[int, _Function],
    relations: Mapping[int, tuple[str, str]],
    types: Mapping[int, tuple[str, str]],
    operators: Mapping[int, tuple[str, str]],
) -> tuple[set[int], set[int]]:
    """(actor, open): every object whose evaluation observes the actor; open
    excludes relations that observe it only through their policies."""
    open_actor = set(graph.reads)
    actor = open_actor | graph.policy_reads
    changed = True
    while changed:
        changed = False
        function_names = {functions[oid].name for oid in actor if oid in functions}
        type_names = {types[oid][1] for oid in actor if oid in types}
        relation_all = {relations[oid][1] for oid in actor if oid in relations}
        relation_open = {relations[oid][1] for oid in open_actor if oid in relations}
        symbols = {operators[oid][1] for oid in actor if oid in operators}
        for owner, targets in graph.evaluates.items():
            if owner in open_actor:
                continue
            function = functions.get(owner)
            if targets & (open_actor if function and function.blind else actor):
                open_actor.add(owner)
                actor.add(owner)
                changed = True
        for owner, targets in graph.policies.items():
            if owner not in actor and targets & actor:
                actor.add(owner)
                changed = True
        for oid, tokens in graph.tokens.items():
            if oid in open_actor:
                continue
            seen = relation_open if functions[oid].blind else relation_all
            if tokens & (function_names | type_names | seen) or any(
                symbol in graph.code[oid] for symbol in symbols
            ):
                open_actor.add(oid)
                actor.add(oid)
                changed = True
    return actor, open_actor


def _binder_closure(
    graph: _Graph,
    functions: Mapping[int, _Function],
    relations: Mapping[int, tuple[str, str]],
    operators: Mapping[int, tuple[str, str]],
) -> set[int]:
    """Every object whose evaluation (triggers included) can bind an actor
    setting."""
    binders = set(graph.binds)
    changed = True
    while changed:
        changed = False
        names = {functions[oid].name for oid in binders if oid in functions} | {
            relations[oid][1] for oid in binders if oid in relations
        }
        for edges in (graph.evaluates, graph.policies, graph.triggers):
            for owner, targets in edges.items():
                if owner not in binders and targets & binders:
                    binders.add(owner)
                    changed = True
        symbols = {operators[oid][1] for oid in binders if oid in operators}
        for oid, tokens in graph.tokens.items():
            if oid not in binders and (
                tokens & names or any(symbol in graph.code[oid] for symbol in symbols)
            ):
                binders.add(oid)
                changed = True
    return binders


def _fail_closed(
    functions: Iterable[_Function],
    actor: set[int],
    settings: frozenset[str],
    runs_text: frozenset[int] = frozenset(),
) -> None:
    for function in functions:
        if function.system:
            continue
        where = f"{function.schema}.{function.name}"
        language = function.language
        if language not in ("sql", "plpgsql", "c", "internal") or (
            language in ("c", "internal") and not function.extension
        ):
            message = f"{where} is opaque ({language}); its actor reads are unknown"
            raise CensusError(message)
        configured = {
            entry.split("=", 1)[0].strip().lower() for entry in function.config
        }
        if configured & settings or (
            language in ("sql", "plpgsql")
            and any(_binding(setting).search(function.body) for setting in settings)
        ):
            message = (
                f"{where} binds an actor setting; a deployed context without an "
                "actor could acquire one inside the database"
            )
            raise CensusError(message)
        if configured & {_STANDARD_STRINGS}:
            message = (
                f"{where} sets {_STANDARD_STRINGS}; the text it runs cannot be "
                "lexed, so its actor reads are unknown"
            )
            raise CensusError(message)
        unsettled = (
            lex(function.body).unsettled if language in ("sql", "plpgsql") else ()
        )
        if unsettled:
            message = (
                f"{where} has a body the lexer cannot settle ({'; '.join(unsettled)}); "
                "its actor reads are unknown"
            )
            raise CensusError(message)
        if language == "plpgsql" and _DYNAMIC.search(_bare(function.body)):
            message = f"{where} uses dynamic SQL; its actor reads are unknown"
            raise CensusError(message)
        if language in ("sql", "plpgsql") and function.oid in runs_text:
            message = f"{where} runs SQL from text; its actor reads are unknown"
            raise CensusError(message)
        if (
            function.oid in actor
            and language == "sql"
            and not function.definer
            and not function.config
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
    """The builtins that expose session settings (and, in the catalog, the
    views built on them).

    ``named`` take the setting name as their first argument; ``enumerators``
    return every setting at once, so any use of them is unresolvable.
    Derived from ``pg_proc`` (internal builtins whose C symbol handles a
    configuration setting by name or all settings); stored expressions name
    them by oid.
    """

    named: frozenset[str]
    enumerators: frozenset[str]
    named_oids: Mapping[int, str] = field(default_factory=dict)
    enumerator_oids: frozenset[int] = frozenset()
    # Named builtins that set the setting (``set_config``).
    setter_oids: frozenset[int] = frozenset()
    # Symbols of operators whose function is one of the readers.
    operators: frozenset[str] = frozenset()
    # Builtins that run SQL handed to them as text (``query_to_xml``,
    # ``ts_stat``, ...): what that SQL reads or binds cannot be read.
    executors: frozenset[str] = frozenset()
    executor_oids: frozenset[int] = frozenset()


# C symbols of the backend's setting accessors (show/set by name, show all).
_READER_SYMBOL: Final = re.compile(
    r"config_by_name|all_settings|settings_get_flags|all_file_settings"
)
_SETTER_SYMBOL: Final = re.compile(r"^set_config_by_name$")


def setting_readers(cursor: CursorWrapper) -> SettingReaders:
    """Derive every builtin that reads or sets a session setting, and every
    builtin that runs SQL given as text: a text input named ``query``, or a
    C symbol naming a query (``query_to_xml``, ``tsquery_rewrite_query``)."""
    cursor.execute(
        "SELECT p.oid, p.proname, p.prosrc, COALESCE(p.proargtypes[0], 0) "
        "  = 'pg_catalog.text'::pg_catalog.regtype, "
        "'pg_catalog.text'::pg_catalog.regtype = ANY(p.proargtypes::pg_catalog.oid[]) "
        "  AND ('query' = ANY(p.proargnames[1:p.pronargs]) "
        "    OR p.prosrc ~ '(^|_)query(_|$)') "
        "FROM pg_catalog.pg_proc p "
        "JOIN pg_catalog.pg_language l ON l.oid = p.prolang "
        "WHERE p.pronamespace = 'pg_catalog'::pg_catalog.regnamespace "
        "AND l.lanname IN ('internal', 'c')"
    )
    named: dict[int, str] = {}
    enumerators: dict[int, str] = {}
    executors: dict[int, str] = {}
    setters: set[int] = set()
    for oid, name, symbol, takes_name, runs_text in cursor.fetchall():
        if _READER_SYMBOL.search(str(symbol)):
            (named if takes_name else enumerators)[int(oid)] = str(name)
            if _SETTER_SYMBOL.search(str(symbol)):
                setters.add(int(oid))
        elif runs_text:
            executors[int(oid)] = str(name)
    return SettingReaders(
        frozenset(named.values()),
        frozenset(enumerators.values()),
        named,
        frozenset(enumerators),
        frozenset(setters),
        executors=frozenset(executors.values()),
        executor_oids=frozenset(executors),
    )


def _executor_calls(text: str, readers: SettingReaders) -> list[str]:
    """The SQL-text executors a text calls."""
    code = _bare(text)
    return [
        name
        for name in sorted(readers.executors)
        if re.search(rf'(?<![\w$])"?{re.escape(name)}"?\s*\(', code)
    ]


def _unreadable(text: str, readers: SettingReaders) -> list[str]:
    """Text whose meaning the observer cannot read: text the lexer cannot
    settle, SQL run from text, a DO block running dynamic SQL, a
    Unicode-escaped identifier or string."""
    code = _bare(text)
    found = [
        f"cannot be lexed ({reason}), so the observer cannot read it"
        for reason in lex(text).unsettled
    ]
    found.extend(
        f"runs SQL from text via {name}, which the observer cannot read"
        for name in _executor_calls(text, readers)
    )
    if re.match(r"\s*do\b", code) and _DYNAMIC.search(code):
        found.append("runs dynamic SQL in a DO block, which the observer cannot read")
    if "u&" in code:
        found.append(
            "uses a Unicode-escaped identifier or string, which the observer "
            "cannot read"
        )
    return found


_PLACEHOLDER: Final = re.compile(r"%(?:\((?P<key>[^)]+)\))?s|\$(?P<number>[1-9]\d*)")
_ARGUMENT: Final = re.compile(
    r"\s*(?:'(?P<literal>(?:[^']|'')*+)'"
    r"|(?P<placeholder>%(?:\([^)]+\))?s|\$[1-9]\d*))"
    r"(?:\s*::\s*[a-z_ .]+)?\s*(?P<end>[,)])"
)
_SHOW: Final = re.compile(r"^\s*show\s+(?P<name>[a-z_][a-z0-9_.]*)", re.IGNORECASE)
_SET: Final = re.compile(
    r"^\s*set\s+(?:session\s+|local\s+)?(?P<name>[a-z_][a-z0-9_.]*)\s*(?:=|to)\s*"
    r"(?P<value>.*?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _parameter(sql_text: str, position: int, token: str, params: object) -> object:
    """The value a psycopg placeholder at ``position`` binds, or a sentinel.

    ``sql_text`` is the statement as sent: psycopg numbers placeholders
    without reading SQL, so one inside a comment or a literal counts too."""
    key = _PLACEHOLDER.fullmatch(token)
    if key is not None and key.group("number") is not None:
        index = int(key.group("number")) - 1
        value = (
            params[index]
            if isinstance(params, list | tuple) and index < len(params)
            else _UNRESOLVED
        )
        if isinstance(value, bytes | bytearray | memoryview):
            try:
                return bytes(value).decode()
            except UnicodeDecodeError:
                return _UNRESOLVED
        return value
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


def _arguments(
    code: str, sql_text: str, start: int, params: object, count: int
) -> list[object]:
    """Resolve up to ``count`` leading arguments of a call opened at ``start``
    in ``code`` (the lexer's view of ``sql_text``, position for position)."""
    values: list[object] = []
    position = start
    for _ in range(count):
        match = _ARGUMENT.match(code, position)
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
    lexed = lex(sql_text)
    code = lexed.code
    calls: list[tuple[str, list[object]]] = []
    for reader in sorted(readers.named):
        pattern = re.compile(rf'(?<![\w$])"?{re.escape(reader)}"?\s*\(', re.IGNORECASE)
        calls.extend(
            (reader, _arguments(code, sql_text, match.end(), params, 2))
            for match in pattern.finditer(code)
        )
    lowered = lexed.bare.lower()
    calls.extend(
        (reader, [_UNRESOLVED])
        for reader in sorted(readers.enumerators)
        if re.search(rf'(?<![\w$])"?{re.escape(reader)}"?(?![\w$])', lowered)
    )
    calls.extend(
        (f"operator {symbol}", [_UNRESOLVED])
        for symbol in sorted(readers.operators)
        if symbol in lowered
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
            resolved = _parameter(
                sql_text, setting.start("value"), value.strip(), params
            )
        calls.append(("set", [setting.group("name").lower(), resolved]))
    return calls


def _changes_lexing(reader: str, arguments: list[object]) -> bool:
    """A set or ``set_config`` whose resolved name is
    ``standard_conforming_strings`` (a name spelled in the text is already
    unsettled; this is the one a bound parameter carries)."""
    return (
        reader in ("set_config", "set")
        and bool(arguments)
        and isinstance(arguments[0], str)
        and arguments[0].strip().lower() == _STANDARD_STRINGS
    )


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

    @property
    def stack(self) -> tuple[str, ...]:
        """Qualified names of the apps.* frames that sent it, innermost first."""
        names: list[str] = []
        for frame, _ in traceback.walk_stack(self.frame):
            module = frame.f_globals.get("__name__", "")
            if isinstance(module, str) and module.startswith("apps."):
                names.append(f"{module}.{frame.f_code.co_qualname}")
        return tuple(names)


# Execution messages (the only frontend messages that make the server run
# anything), the start of every traced message, and COPY.
_EXECUTION: Final = re.compile(
    rb"^F\t\d+\t(?:Query|Execute|FunctionCall)(?![A-Za-z])", re.MULTILINE
)
_HEADER: Final = re.compile(rb"^([FB])\t\d+\t", re.MULTILINE)
_COPY: Final = re.compile(rb"^B\t\d+\tCopy(?:In|Out|Both)Response", re.MULTILINE)
_TRACE_LIMIT: Final = 64 * 1024 * 1024
_LIBC: Final = ctypes.CDLL(None)


class WireTrace:
    """libpq's protocol trace of one connection (``PQtrace``).

    libpq writes every message it composes for the server, or parses from
    it, on this connection, whichever Python path asked for it: the ground
    truth of what the probed session sent and received. The trace stream
    buffers in C, so reading flushes every C stream first. The writer
    descriptor belongs to that stream from here on.
    """

    def __init__(self, pgconn: PGconn) -> None:
        handle, path = tempfile.mkstemp(prefix="actor-wire-")
        try:
            self._writer = os.open(path, os.O_WRONLY | os.O_APPEND)
            self._reader = os.open(path, os.O_RDONLY)
        finally:
            os.close(handle)
            Path(path).unlink()
        self.pgconn = pgconn
        pgconn.trace(self._writer)
        pgconn.set_trace_flags(pq.Trace.SUPPRESS_TIMESTAMPS)
        self._start = 0

    def mark(self) -> None:
        """Start a new window at the end of what is traced so far."""
        _LIBC.fflush(None)
        size = os.fstat(self._reader).st_size
        if size > _TRACE_LIMIT:
            os.ftruncate(self._writer, 0)
            size = 0
        self._start = size

    def since_mark(self) -> bytes:
        """Everything traced since the last mark."""
        _LIBC.fflush(None)
        end = os.fstat(self._reader).st_size
        return os.pread(self._reader, end - self._start, self._start)

    def close(self) -> None:
        self.pgconn.untrace()
        os.close(self._reader)


def _needles(values: Iterable[str]) -> tuple[bytes, ...]:
    """The actor ids as the trace shows them (text, and binary as libpq
    prints message bytes: printable as is, the rest as ``\\xNN``)."""
    found: set[bytes] = set()
    for value in values:
        found.update({value.encode(), value.upper().encode()})
        raw = _uuid_bytes(value)
        if raw:
            found.add(
                b"".join(
                    bytes([byte]) if 0x20 <= byte < 0x7F else b"\\x%02x" % byte
                    for byte in raw
                )
            )
    return tuple(sorted(found))


def _value_flow(chunk: bytes, needles: Iterable[bytes]) -> list[str]:
    """The actor id crossing the wire, by direction."""
    positions: set[int] = set()
    for needle in needles:
        position = chunk.find(needle)
        while position != -1:
            positions.add(position)
            position = chunk.find(needle, position + 1)
    if not positions:
        return []
    heads = [(match.start(), match.group(1)) for match in _HEADER.finditer(chunk)]
    starts = [start for start, _ in heads]
    found: set[str] = set()
    for position in positions:
        index = bisect.bisect_right(starts, position) - 1
        if index >= 0 and heads[index][1] == b"B":
            found.add("statement receives the actor id (on the wire)")
        else:
            found.add("statement sends the actor id (on the wire)")
    return sorted(found)


@dataclass(slots=True)
class ActorObserver:
    """Collect actor observations for one execution at a time.

    Modes (``begin``):
    - ``bound``: the deployed staff context, an actor is bound. Every
      channel counts, including behaviour: the bound actor's id appearing in
      a statement's text or parameters, anything the server sends back or
      the outcome.
    - ``unbound``: a deployed patient or sessionless context, no actor
      bound. Every read then sees the same unbound value, so what counts is
      acquiring one: a statement binding an actor setting (name and value
      resolved at run time, unresolvable counts), a statement using a
      derived binder, or one bound at the end.
    - ``injected``: a matrix state that injects an actor into such a
      context. The injection is the test's, so only binding counts.
    Reads of a staff request's user, COPY, and an execution the server
    received that no recorded send explains count in every mode.
    """

    catalog: ActorCatalog
    accessors: Mapping[CodeType, str]
    statements: list[_Statement] = field(default_factory=list)
    entered: list[str] = field(default_factory=list)
    blind: list[str] = field(default_factory=list)
    active: bool = False
    mode: str = "bound"
    values: frozenset[str] = frozenset()
    wire: WireTrace | None = None
    _needles: tuple[bytes, ...] = ()
    _sent: int = 0
    _executions: int = 0
    _functions: dict[int, int] = field(default_factory=dict)
    _tables: dict[int, int] = field(default_factory=dict)
    # The last bound snapshot, reusable as the next execution's starting
    # point while the scope transaction is unchanged (``carry``/``drop``).
    _carried: _Snapshot | None = None
    # Prepared statements of the session by name: (reads, acquires or hides).
    _prepared: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    _classified: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    # Findings about the session state itself, for the current execution.
    _session: list[str] = field(default_factory=list)
    _failed: tuple[frozenset[int], str] | None = None
    _may_bind: re.Pattern[str] = field(init=False)
    _symbols: tuple[str, ...] = field(init=False)
    _by_name: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self._derive()

    def _derive(self) -> None:
        """What the statement checks need from the catalog."""
        words = sorted(name for name in self.catalog.binders if _TOKEN.fullmatch(name))
        self._symbols = tuple(
            sorted(name for name in self.catalog.binders if not _TOKEN.fullmatch(name))
        )
        executors = sorted(self.catalog.readers.executors)
        self._may_bind = re.compile(
            "|".join(
                [r"set_config", r"^\s*set\s", r"^\s*do\b", r"\bexecute\b", r"u&"]
                + [rf"(?<![\w$]){re.escape(word)}(?![\w$])" for word in words]
                + [rf"(?<![\w$]){re.escape(name)}(?![\w$])" for name in executors]
                + [re.escape(symbol) for symbol in self._symbols]
            ),
            re.IGNORECASE,
        )
        self._by_name = {
            name: self.catalog.qualified.get(oid, f"clinic_app.{name}")
            for oid, name in self.catalog.functions.items()
        }
        self._classified.clear()

    def _session_state(self) -> tuple[int, int, str]:
        """Whether the session holds state a statement can reach without
        naming what it does: its temporary schema (0 when it has none), its
        prepared statements (how many), and how it lexes string literals
        (``standard_conforming_strings``). Read for every execution, one
        libpq round trip outside the window; a bound execution reads the same
        columns with its statistics snapshot."""
        ((schema, prepared, standard),) = self._rows(_SESSION_STATE)
        return int(schema), int(prepared), standard

    def _rows(self, query: bytes) -> list[tuple[str, ...]]:
        """Rows of an observer query sent through libpq, outside the window."""
        assert self.wire is not None
        result = self.wire.pgconn.exec_(query)
        if result.status != pq.ExecStatus.TUPLES_OK:
            message = f"the session state cannot be read: {result.error_message!r}"
            raise AssertionError(message)
        return [
            tuple(
                (result.get_value(row, column) or b"").decode()
                for column in range(result.nfields)
            )
            for row in range(result.ntuples)
        ]

    def _apply_session(self, schema: int, prepared: int, standard: str) -> bool:
        """Take in the session state before this execution: the temporary
        schema's objects (one the catalog was not derived with makes it derive
        again; one it cannot observe fails closed) and every prepared
        statement's text, classified like a statement; a session that lexes
        string literals with ``standard_conforming_strings`` off is a finding
        of its own. True when the catalog changed."""
        temporary = (
            frozenset(int(oid) for (oid,) in self._rows(_TEMPORARY_OBJECTS.encode()))
            if schema
            else frozenset()
        )
        derived = False
        if not temporary <= self.catalog.temporary:
            derived = self._rederive(temporary)
        self._session = (
            [self._failed[1]]
            if self._failed is not None and self._failed[0] == temporary
            else []
        )
        if standard != "on":
            self._session.append(
                f"the session runs with {_STANDARD_STRINGS} {standard!r}, so its "
                "text cannot be lexed"
            )
        self._prepared = {
            name: self._text_reasons(text)
            for name, text in (self._rows(_PREPARED_STATEMENTS) if prepared else [])
        }
        return derived

    def _rederive(self, temporary: frozenset[int]) -> bool:
        if self._failed is not None and self._failed[0] == temporary:
            return False
        try:
            with connection.cursor() as cursor:
                catalog = actor_catalog(cursor, self.catalog.settings)
        except CensusError as error:
            self._failed = (
                temporary,
                f"the session's temporary objects cannot be observed: {error}",
            )
            return False
        self._failed = None
        self.catalog = catalog
        self._carried = None
        self._derive()
        return True

    def _text_reasons(self, text: str) -> tuple[list[str], list[str]]:
        """What a stored text (a prepared statement) reads, and what it
        acquires or hides, against the derived reader set."""
        if text in self._classified:
            return self._classified[text]
        settings = self.catalog.settings
        reads: list[str] = []
        acquires = list(_unreadable(text, self.catalog.readers))
        for reader, arguments in setting_calls(text, None, self.catalog.readers):
            if (
                reader in ("set_config", "set")
                and arguments
                and _named(arguments[0], settings)
            ):
                acquires.append("binds an actor or unresolvable setting")
            elif _changes_lexing(reader, arguments):
                acquires.append(
                    f"sets {_STANDARD_STRINGS}, so later text cannot be lexed"
                )
            elif reader != "set" and (not arguments or _named(arguments[0], settings)):
                reads.append(f"reads an actor or unresolvable setting via {reader}")
        tokens = _tokens(text)
        code = _bare(text)
        reads.extend(
            f"calls {self._by_name[name]}"
            for name in sorted(tokens & set(self._by_name))
        )
        reads.extend(
            f"touches actor relation {name}"
            for name in sorted(tokens & self.catalog.relations)
        )
        reads.extend(
            f"uses actor type {name}" for name in sorted(tokens & self.catalog.types)
        )
        reads.extend(
            f"uses actor operator {symbol}"
            for symbol in sorted(self.catalog.operators)
            if symbol in code
        )
        acquires.extend(
            f"may bind an actor setting through {name}"
            for name in sorted(tokens & self.catalog.binders)
        )
        acquires.extend(
            f"may bind an actor setting through {symbol}"
            for symbol in self._symbols
            if symbol in code
        )
        self._classified[text] = (reads, acquires)
        return reads, acquires

    def _snapshot(
        self,
    ) -> _Snapshot:
        """One statement: watched call counts, actor relation touches, the
        bound actor values (per-oid statistics, not the full views), and the
        session state (``_session_state``)."""
        self.active = False
        relations = self.catalog.relation_oids
        with connection.cursor() as cursor:
            cursor.execute(
                _SNAPSHOT,
                [
                    list(self.catalog.watched),
                    [oid for oid, _ in relations],
                    sorted(self.catalog.settings),
                ],
            )
            row = cursor.fetchone()
        assert row is not None
        calls, touches, values, tracking, schema, prepared, standard = row
        assert tracking == "all", "track_functions must be all"
        return (
            dict(zip(self.catalog.watched, map(int, calls), strict=True)),
            {
                oid: int(count)
                for (oid, _), count in zip(relations, touches, strict=True)
            },
            [str(value) for value in values if value],
            int(schema),
            int(prepared),
            str(standard),
        )

    def begin(self, *, mode: str, actor: str | None = None) -> None:
        assert self.wire is not None, "the observer runs inside statements_captured"
        self.mode = mode
        if mode == "injected":
            assert actor, "an injected execution names its injected actor"
            self.values = frozenset({actor})
        if mode == "bound":
            # A carried snapshot is the previous execution's end: nothing runs
            # between two inputs of a scope, so it is this one's start.
            snapshot, self._carried = self._carried or self._snapshot(), None
            if self._apply_session(*snapshot[3:]):
                snapshot = self._snapshot()
            self._functions, self._tables, values, *_ = snapshot
            assert values, "a bound execution needs a bound actor"
            self.values = frozenset(values)
        if mode != "bound":
            self._apply_session(*self._session_state())
        if mode == "unbound":
            assert not self._bound_values(), "the deployed context binds an actor"
            self.values = frozenset()
        self._needles = _needles(self.values) if mode == "bound" else ()
        self.statements.clear()
        self.entered.clear()
        self.blind.clear()
        _READS.clear()
        self._sent = 0
        self._executions = 0
        self.wire.mark()
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
        self._close_segment()
        found: list[str] = list(self._session)
        for statement in self.statements:
            found.extend(self._binds(statement))
        if self._executions != self._sent:
            found.append(
                f"the server received {self._executions} statement(s) on the probed "
                f"connection and the observer recorded {self._sent}: a path outside "
                "the recorded driver calls reached it"
            )
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

    def _close_segment(self) -> None:
        """Account for what the wire carried since the window (re)opened."""
        assert self.wire is not None
        chunk = self.wire.since_mark()
        self._executions += len(_EXECUTION.findall(chunk))
        if _COPY.search(chunk):
            self.blind.append("uses COPY, which the observer cannot inspect")
        if self._needles:
            self.blind.extend(_value_flow(chunk, self._needles))

    def _binds(self, statement: _Statement) -> list[str]:
        """A statement acquires an actor: it binds an actor setting (or one
        it cannot resolve) to a value other than empty or the actor already
        bound (a lifecycle helper restoring the saved value acquires none),
        or it uses a derived binder."""
        if (
            not self._may_bind.search(statement.sql)
            and not lex(statement.sql).unsettled
        ):
            return []
        found = [
            f"statement {reason} from {statement.stack[:1]}"
            for reason in _unreadable(statement.sql, self.catalog.readers)
        ]
        for name in _executed(statement.sql):
            reads, acquires = self._prepared.get(name, ([], []))
            found.extend(
                f"statement executes prepared statement {name}, which {reason}"
                for reason in acquires + (reads if self.mode == "bound" else [])
            )
        found += [
            f"statement may bind an actor setting through {name} from "
            f"{statement.stack[:1]}"
            for name in sorted(_tokens(statement.sql) & self.catalog.binders)
        ]
        found.extend(
            f"statement may bind an actor setting through {symbol} from "
            f"{statement.stack[:1]}"
            for symbol in self._symbols
            if symbol in statement.sql
        )
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
                    found.append(f"binds an actor setting from {statement.stack[:1]}")
            elif _changes_lexing(reader, arguments):
                found.append(
                    f"statement sets {_STANDARD_STRINGS}, so later text cannot be "
                    f"lexed, from {statement.stack[:1]}"
                )
        return found

    def drop_carried(self) -> None:
        """A new scope transaction starts: statistics restart from zero."""
        self._carried = None

    def _bound_findings(self) -> list[str]:
        snapshot = self._snapshot()
        # Nothing between two inputs of one scope moves the statistics (the
        # input's savepoint release or rollback does not), so this end is
        # the next input's start.
        self._carried = snapshot
        functions, tables, *_ = snapshot
        found: list[str] = []
        called = {
            oid
            for oid, calls in functions.items()
            if calls > self._functions.get(oid, 0)
        }
        found.extend(
            f"calls {self.catalog.qualified.get(oid, 'clinic_app.' + name)}"
            for oid, name in sorted(self.catalog.functions.items())
            if oid in called
        )
        explained: set[str] = set()
        for oid in called:
            explained |= self.catalog.reads.get(oid, frozenset())
        for statement in self.statements:
            explained |= _tokens(statement.sql) & self.catalog.relation_names
            found.extend(self._statement_findings(statement))
        touched = {
            name
            for oid, name in self.catalog.relation_oids
            if tables.get(oid, 0) > self._tables.get(oid, 0)
        }
        found.extend(
            f"unexplained touch of actor relation {name}"
            for name in sorted(touched - explained)
        )
        found.extend(f"enters {name}" for name in self.entered)
        return found

    def _statement_findings(self, statement: _Statement) -> list[str]:
        settings = self.catalog.settings
        tokens = _tokens(statement.sql)
        actor_functions = set(self._by_name)
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
        sends = any(
            value in statement.sql or value in repr(statement.params)
            for value in self.values
        )
        if sends:
            found.append(f"statement sends the actor id from {statement.stack[:1]}")
        found.extend(
            f"statement calls {self._by_name[name]}"
            for name in sorted(tokens & actor_functions)
        )
        found.extend(
            f"statement touches actor relation {relation}"
            for relation in sorted(tokens & self.catalog.relations)
        )
        found.extend(
            f"statement uses actor type {name}"
            for name in sorted(tokens & self.catalog.types)
        )
        if self.catalog.operators:
            code = lex(statement.sql).bare
            found.extend(
                f"statement uses actor operator {symbol}"
                for symbol in sorted(self.catalog.operators)
                if symbol in code
            )
        sender = next(
            (name for name in statement.stack if name.startswith("apps.identity.")), ""
        )
        if (
            sender
            and (reads or sends or tokens & actor_functions)
            and sender not in set(self.accessors.values())
        ):
            found.append(f"unlisted accessor {sender} reaches the actor")
        return found

    def record(self, sql_text: bytes | str, params: object) -> None:
        """One statement psycopg sent while observing."""
        text = (
            sql_text.decode(errors="replace")
            if isinstance(sql_text, bytes)
            else sql_text
        )
        frame: FrameType | None = sys._getframe(1)  # the sender, resolved on demand
        while frame is not None and _driver_frame(frame):
            frame = frame.f_back
        self.statements.append(_Statement(text, params, frame))

    def sending(self, pgconn: object, pipeline: Any, queued: int) -> None:  # noqa: ANN401 - psycopg's pipeline
        """A send call returned: count its execution message now, or when the
        pipeline runs the command it queued. Only the probed connection's
        sends count; the trace is of that connection."""
        if self.wire is None or pgconn is not self.wire.pgconn:
            return
        if not pipeline:
            self._sent += 1
            return
        commands = pipeline.command_queue
        for index in range(queued, len(commands)):
            commands[index] = self._counting(commands[index])

    def _counting(self, command: Callable[[], object]) -> Callable[[], object]:
        def run() -> object:
            result = command()
            if self.active:
                self._sent += 1
            return result

        return run

    def on_start(self, code: CodeType) -> None:
        if self.active and code in self.accessors:
            self.entered.append(self.accessors[code])

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Harness setup inside a probe input (not the probed function): its
        statements, and the wire traffic they cause, are left out."""
        active, self.active = self.active, False
        if active:
            self._close_segment()
        try:
            yield
        finally:
            if active and self.wire is not None:
                self.wire.mark()
            self.active = active


# The session state read for every execution: whether the session has a
# temporary schema, how many prepared statements (SQL or protocol level), and
# standard_conforming_strings; then, only when there are any, the schema's
# objects and the statements' text.
_SESSION_COLUMNS: Final = sql.SQL(
    "pg_catalog.pg_my_temp_schema(), "
    "(SELECT pg_catalog.count(*) FROM pg_catalog.pg_prepared_statements), "
    "pg_catalog.current_setting('standard_conforming_strings')"
)
_SESSION_STATE: Final = sql.SQL("SELECT {}").format(_SESSION_COLUMNS).as_bytes(None)
_PREPARED_STATEMENTS: Final = (
    b"SELECT name, statement FROM pg_catalog.pg_prepared_statements"
)
# A bound execution's statistics snapshot, with the session state.
_SNAPSHOT: Final = (
    sql.SQL(
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
        "pg_catalog.current_setting('track_functions'), {session}"
    )
    .format(session=_SESSION_COLUMNS)
    .as_string(None)
)
_EXECUTED: Final = re.compile(
    r'\bexecute\s+("(?:[^"]|"")+"|[a-z_][a-z0-9_$]*)', re.IGNORECASE
)


def _executed(text: str) -> list[str]:
    """Names of the prepared statements a statement executes (SQL EXECUTE)."""
    code = lex(text).bare
    return [
        name[1:-1].replace('""', '"') if name.startswith('"') else name.lower()
        for name in _EXECUTED.findall(code)
    ]


def _driver_frame(frame: FrameType) -> bool:
    module = frame.f_globals.get("__name__", "")
    return isinstance(module, str) and (
        module.startswith("psycopg") or module == __name__
    )


def _uuid_bytes(value: str) -> bytes:
    try:
        return UUID(value).bytes
    except ValueError:
        return b""


def _command_text(command: object, context: object) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, bytes):
        return command.decode(errors="replace")
    if isinstance(command, sql.Composable):
        return command.as_string(cast("Any", context))
    return str(command)


def _queued(pipeline: Any) -> int:  # noqa: ANN401 - psycopg's pipeline
    return len(pipeline.command_queue) if pipeline else 0


def _observed_send(
    observer: ActorObserver, original: Callable[..., None], position: int
) -> Callable[..., None]:
    """A cursor send call (query at ``position``) that records what it sent.

    It records only once the call returned: the libpq send is its last step,
    so a call that raised sent nothing.
    """

    def run(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - psycopg internals
        if not observer.active:
            original(self, *args, **kwargs)
            return
        conn = self._conn
        queued = _queued(conn._pipeline)
        original(self, *args, **kwargs)
        query = args[position]
        observer.record(query.query, query.params)
        observer.sending(conn.pgconn, conn._pipeline, queued)

    return run


def _observed_command(
    observer: ActorObserver, original: Callable[..., Generator[Any, Any, Any]]
) -> Callable[..., Generator[Any, Any, Any]]:
    """The connection's command generator, recorded; its send happens
    before its first wait, so it counts once that first step completed."""

    def run(
        self: Any,  # noqa: ANN401 - psycopg internals
        command: object,
        result_format: pq.Format = pq.Format.TEXT,
    ) -> Generator[Any, Any, Any]:
        if not observer.active:
            return (yield from original(self, command, result_format))
        queued = _queued(self._pipeline)
        observer.record(_command_text(command, self), None)
        steps = original(self, command, result_format)
        try:
            first = next(steps)
        except StopIteration as done:
            observer.sending(self.pgconn, self._pipeline, queued)
            return done.value
        observer.sending(self.pgconn, self._pipeline, queued)
        return (yield from _resumed(steps, first))

    return run


def _resumed(
    steps: Generator[Any, Any, Any], value: object
) -> Generator[Any, Any, Any]:
    """Continue a generator already advanced once (``yield from`` semantics)."""
    while True:
        try:
            received = yield value
        except GeneratorExit:
            steps.close()
            raise
        except BaseException as error:  # noqa: BLE001 - delegated to the generator
            try:
                value = steps.throw(error)
            except StopIteration as done:
                return done.value
        else:
            try:
                value = steps.send(received)
            except StopIteration as done:
                return done.value


def _owners(roots: Iterable[type], name: str) -> list[type]:
    """Every class of the roots' hierarchies that defines ``name`` itself."""
    owners: list[type] = []
    for root in roots:
        for klass in root.__mro__:
            if name in vars(klass) and klass not in owners:
                owners.append(klass)
    return owners


@contextmanager
def statements_captured(observer: ActorObserver) -> Iterator[None]:
    """Record every statement psycopg sends while observing, and trace the
    probed connection's protocol as the ground truth to check them against.

    The send calls are psycopg's own (every cursor class sends through
    ``_execute_send``/``_send_query_prepared``, the connection through
    ``_exec_command``); a path that avoids them is not missed, since libpq
    traces its message all the same and the counts disagree.
    """
    connection.ensure_connection()
    driver = cast("Any", connection.connection)
    cursors = [
        psycopg.Cursor,
        psycopg.ClientCursor,
        psycopg.ServerCursor,
        psycopg.RawCursor,
        driver.cursor_factory,
        driver.server_cursor_factory,
    ]
    originals: dict[tuple[type, str], Callable[..., Any]] = {}
    for name, position in (("_execute_send", 0), ("_send_query_prepared", 1)):
        for owner in _owners(cursors, name):
            originals[(owner, name)] = vars(owner)[name]
            setattr(owner, name, _observed_send(observer, vars(owner)[name], position))
    command = "_exec_command"
    for owner in _owners([type(driver), psycopg.Connection], command):
        originals[(owner, command)] = vars(owner)[command]
        setattr(owner, command, _observed_command(observer, vars(owner)[command]))
    wire = WireTrace(driver.pgconn)
    observer.wire = wire
    try:
        yield
    finally:
        observer.wire = None
        wire.close()
        for (owner, name), method in originals.items():
            setattr(owner, name, method)
