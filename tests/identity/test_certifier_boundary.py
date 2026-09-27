"""The actor rule's boundary: what it proves, what it does not, and the
syntax it cannot fully see, refused in the code it certifies.

What the actor rule (identity/actor_channels.py) proves, for every observed
execution (every state, every deployed run):
- stored expressions exactly: every pg_node_tree column of pg_catalog
  (``EXPRESSION_COLUMNS``, pinned to the live catalog), by oid, in every
  schema including the session's temporary one;
- text (client statements, sql/plpgsql bodies, prepared statements) against
  the derived reader set: the setting builtins, the views over them, every
  operator whose function is one of them, derived actor and binding objects;
- session state read for every execution: prepared statements and the
  temporary schema;
- the wire: every execution the probed connection's libpq sent matches a
  recorded send (the actor id is searched in both directions).

What it does not prove:
- what the server evaluated: the wire trace proves what psycopg sent;
- text the server parses is classified by an approximation of PostgreSQL's
  parser, not by the parser;
- stock PostgreSQL 16 records no calls to builtins (``current_setting``), so
  there is no server-side ground truth for a builtin read;
- a hand-written wire protocol on libpq's raw socket is outside the trace.

The approximation is bounded by enforcement, not by parser completeness:
``UNHANDLED`` declares, one key per class, the syntax the classifier cannot
fully see, and the guard refuses each anywhere in ``apps/`` (runtime code and
migrations), naming the class and the file and line. The declaration is
pinned: every class has a planted example the guard refuses under its own
key, and the guard emits no undeclared key, so the boundary cannot widen
silently. Reader, executor, operator and function names come from the live
catalog (``actor_channels.actor_catalog``), not from a list.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from identity import actor_channels
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

# The syntax classes text classification cannot fully see, refused in apps/.
UNHANDLED: Final[Mapping[str, str]] = {
    "operator-alias": (
        "CREATE OPERATOR over a setting reader, a SQL-text executor or a derived "
        "actor-observing or binding function"
    ),
    "server-prepare": "server-side PREPARE",
    "dynamic-execute": (
        "EXECUTE of SQL text or of a prepared statement (plpgsql, DO, SQL EXECUTE)"
    ),
    "temporary-object": "an object created in pg_temp (CREATE TEMP/TEMPORARY)",
    "pg-temp-name": "a name qualified with pg_temp",
    "pg-temp-search-path": "a search_path listing pg_temp before another schema",
    "unresolvable-name": (
        "a setting read or bound through a name that is neither a literal nor a "
        "client placeholder"
    ),
    "enumerating-reader": "every setting at once (pg_settings and its views, SHOW ALL)",
    "reader-operator": "a setting read through an operator over a reader",
    "text-executor": "SQL run from text (a derived executor: query_to_xml, ...)",
    "unicode-escape": "a Unicode-escaped identifier or string",
    "libpq-direct": "libpq reached directly (.pgconn)",
    "raw-socket": "the database connection's raw socket",
}

APPS: Final = Path(__file__).resolve().parents[2] / "apps"
# A Python value spliced into SQL text: unknown to the scanner.
_HOLE: Final = "\x00"
_COMMENT: Final = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_LITERAL: Final = re.compile(r"'(?:[^']|'')*'")
# The whole first argument: a literal, or a client placeholder (resolved by
# the observer from the bound value at run time).
_RESOLVABLE: Final = re.compile(
    r"\s*(?:'(?:[^']|'')*'|%s|%\([^)]+\)s)\s*(?:::\s*[a-z_ .]+)?\s*[,)]",
    re.IGNORECASE,
)
_CREATE_OPERATOR: Final = re.compile(
    r"\bcreate\s+operator\s+(?!class\b|family\b)[^(]*\(([^)]*)\)", re.IGNORECASE
)
_OPERATOR_FUNCTION: Final = re.compile(
    r"\b(?:function|procedure)\s*=\s*([\w.\"$]+)", re.IGNORECASE
)
_PREPARE: Final = re.compile(r"\bprepare\s+(?!transaction\b)[a-z_\"]", re.IGNORECASE)
_EXECUTE: Final = re.compile(
    r"\bexecute(?:\s++(?!function\b|procedure\b|on\b)|\s*$)", re.IGNORECASE
)
_TEMPORARY: Final = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?(?:(?:global|local)\s+)?(?:temp|temporary)\b",
    re.IGNORECASE,
)
_PG_TEMP_NAME: Final = re.compile(r"\bpg_temp\s*\.", re.IGNORECASE)
_SEARCH_PATH: Final = re.compile(
    r'\bsearch_path\s*(?:=|\bto\b)\s*((?:"[^"]+"|[\w$]+)(?:\s*,\s*(?:"[^"]+"|[\w$]+))*)',
    re.IGNORECASE,
)
_SEARCH_PATH_CONFIG: Final = re.compile(
    r"set_config\s*\(\s*'search_path'\s*,\s*'([^']*)'", re.IGNORECASE
)
_SHOW_ALL: Final = re.compile(r"\bshow\s+all\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Derived:
    """The reader catalog the guard's patterns come from."""

    # Builtins that read or set a setting by name.
    named: frozenset[str]
    # Readers of every setting, and the views built on them.
    enumerators: frozenset[str]
    # Builtins that run SQL handed to them as text.
    executors: frozenset[str]
    # Symbols of operators over readers or actor-observing functions.
    operators: frozenset[str]
    # Actor-observing and binding functions.
    functions: frozenset[str]


def actor_settings(rbac_graph: RbacGraph) -> frozenset[str]:
    """The settings the staff scope binds to the actor (derived live)."""
    with runtime_role():
        return actor_channels.actor_settings(
            lambda: tenant_context(rbac_graph.physician, rbac_graph.organization_a),
            rbac_graph.physician,
        )


def derived(settings: frozenset[str]) -> Derived:
    with connection.cursor() as cursor:
        catalog = actor_channels.actor_catalog(cursor, settings)
    readers = catalog.readers
    return Derived(
        named=readers.named,
        enumerators=readers.enumerators,
        executors=readers.executors,
        operators=readers.operators | catalog.operators,
        functions=frozenset(catalog.functions.values())
        | {name for name in catalog.binders if re.fullmatch(r"[a-z_][\w$]*", name)},
    )


def _call(name: str) -> re.Pattern[str]:
    return re.compile(rf'(?<![\w$])"?{re.escape(name)}"?\s*\(', re.IGNORECASE)


def sql_violations(text: str, found: Derived) -> list[tuple[str, str]]:
    """What one SQL-bearing text does that the classifier cannot fully see."""
    code = _COMMENT.sub(" ", text)
    bare = _LITERAL.sub(" ", code)
    return [
        *_operator_violations(bare, found),
        *_session_violations(code, bare),
        *_reader_violations(code, bare.lower(), found),
    ]


def _operator_violations(bare: str, found: Derived) -> list[tuple[str, str]]:
    """CREATE OPERATOR over a reader, executor or derived function."""
    readers = found.named | found.enumerators | found.executors | found.functions
    refused: list[tuple[str, str]] = []
    for match in _CREATE_OPERATOR.finditer(bare):
        function = _OPERATOR_FUNCTION.search(match.group(1))
        name = function.group(1).split(".")[-1].strip('"').lower() if function else ""
        if not name or name in readers:
            refused.append(("operator-alias", name or "an unnamed function"))
    return refused


def _session_violations(code: str, bare: str) -> list[tuple[str, str]]:
    """Prepared statements, dynamic SQL, and pg_temp."""
    checks = (
        (_PREPARE, "server-prepare"),
        (_EXECUTE, "dynamic-execute"),
        (_TEMPORARY, "temporary-object"),
        (_PG_TEMP_NAME, "pg-temp-name"),
    )
    refused = [
        (key, match.group(0).strip())
        for pattern, key in checks
        if (match := pattern.search(bare))
    ]
    paths = [match.group(1) for match in _SEARCH_PATH.finditer(bare)]
    paths += [match.group(1) for match in _SEARCH_PATH_CONFIG.finditer(code)]
    for path in paths:
        entries = [entry.strip().strip('"').lower() for entry in path.split(",")]
        if "pg_temp" in entries and entries[-1] != "pg_temp":
            refused.append(("pg-temp-search-path", ", ".join(entries)))
    return refused


def _reader_violations(code: str, lower: str, found: Derived) -> list[tuple[str, str]]:
    """Setting reads the observer cannot resolve, and text it cannot read."""
    refused = [
        ("unresolvable-name", name)
        for name in sorted(found.named)
        for call in _call(name).finditer(code)
        if not _RESOLVABLE.match(code, call.end())
    ]
    refused.extend(
        ("enumerating-reader", name)
        for name in sorted(found.enumerators)
        if re.search(rf'(?<![\w$])"?{re.escape(name)}"?(?![\w$])', lower)
    )
    if _SHOW_ALL.search(lower):
        refused.append(("enumerating-reader", "SHOW ALL"))
    refused.extend(
        ("reader-operator", symbol)
        for symbol in sorted(found.operators)
        if symbol in lower
    )
    refused.extend(
        ("text-executor", name)
        for name in sorted(found.executors)
        if _call(name).search(lower)
    )
    if "u&" in lower:
        refused.append(("unicode-escape", "U&"))
    return refused


def _docstrings(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def _folded(node: ast.AST) -> str | None:
    """A string node's text; concatenations fold, spliced values are holes."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else _HOLE
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else _HOLE) + (
            right if right is not None else _HOLE
        )
    return None


def python_violations(tree: ast.Module) -> Iterator[tuple[int, str, str]]:
    """Python that reaches the server outside psycopg's send calls."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr == "pgconn":
            yield node.lineno, "libpq-direct", ".pgconn"
        elif (
            node.attr in {"fileno", "socket"}
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "connection"
        ):
            yield node.lineno, "raw-socket", f"connection.{node.attr}"


def source_violations(source: str, found: Derived) -> list[tuple[int, str, str]]:
    """(line, class, detail) for every refused construct in one source file."""
    tree = ast.parse(source)
    skipped = _docstrings(tree)
    refused = set(python_violations(tree))
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        text = _folded(node)
        if text is None:
            continue
        line = getattr(node, "lineno", 0)
        refused.update(
            (line, key, detail) for key, detail in sql_violations(text, found)
        )
    return sorted(refused)


def scan(root: Path, found: Derived) -> dict[str, list[tuple[int, str, str]]]:
    return {
        str(path.relative_to(root.parent)): refused
        for path in sorted(root.rglob("*.py"))
        if (refused := source_violations(path.read_text(), found))
    }


def test_the_certified_code_stays_inside_the_boundary(rbac_graph: RbacGraph) -> None:
    """No file under apps/ uses a construct the actor rule cannot fully see."""
    found = derived(actor_settings(rbac_graph))
    refused = scan(APPS, found)
    print(  # noqa: T201 - receipt
        "BOUNDARY",
        len(list(APPS.rglob("*.py"))),
        "files;",
        {
            "named": sorted(found.named),
            "enumerators": sorted(found.enumerators),
            "executors": sorted(found.executors),
            "operators": sorted(found.operators),
            "functions": len(found.functions),
        },
    )
    assert not refused, refused


# At least one planted example per declared class: (class, language, text).
_PLANTED: Final = (
    (
        "operator-alias",
        "sql",
        "CREATE OPERATOR clinic_app.@#@ "
        "(RIGHTARG = text, FUNCTION = pg_catalog.current_setting)",
    ),
    (
        "operator-alias",
        "sql",
        "CREATE OPERATOR clinic_app.### "
        "(LEFTARG = text, RIGHTARG = uuid, FUNCTION = clinic_app.has_permission)",
    ),
    ("server-prepare", "sql", "PREPARE zz(text) AS SELECT $1"),
    ("dynamic-execute", "sql", "EXECUTE zz('x')"),
    ("dynamic-execute", "sql", "DO $$ BEGIN EXECUTE 'SELECT 1'; END $$"),
    ("temporary-object", "sql", "CREATE TEMPORARY TABLE zz (id integer)"),
    ("pg-temp-name", "sql", "SELECT pg_temp.zz()"),
    ("pg-temp-search-path", "sql", "SET search_path = pg_temp, clinic_app"),
    (
        "pg-temp-search-path",
        "sql",
        "SELECT set_config('search_path', 'pg_temp, clinic_app', true)",
    ),
    ("unresolvable-name", "sql", "SELECT current_setting('app.' || 'x')"),
    ("unresolvable-name", "sql", "BEGIN RETURN current_setting(v_name, true); END"),
    ("unresolvable-name", "sql", f"SELECT current_setting({_HOLE})"),
    ("enumerating-reader", "sql", "SELECT setting FROM pg_settings WHERE name = %s"),
    ("enumerating-reader", "sql", "SHOW ALL"),
    ("reader-operator", "sql", "SELECT (OPERATOR(clinic_app.@#@) %s) = %s"),
    ("text-executor", "sql", "SELECT query_to_xml(%s, false, false, '')"),
    ("unicode-escape", "sql", "SELECT U&\"!0063urrent_setting\" UESCAPE '!'('x')"),
    ("libpq-direct", "python", "connection.connection.pgconn.exec_(b'SELECT 1')\n"),
    (
        "raw-socket",
        "python",
        "socket.socket(fileno=connection.connection.fileno())\n",
    ),
)
# A reader operator, derived while it exists (the live catalog has none).
_READER_OPERATOR: Final = (
    "CREATE OPERATOR clinic_app.@#@ "
    "(RIGHTARG = text, FUNCTION = pg_catalog.current_setting)"
)
# What the live code does and the guard must accept.
_CLEAN_SQL: Final = (
    "CREATE FUNCTION clinic_app.f() RETURNS uuid LANGUAGE sql STABLE "
    "SECURITY DEFINER SET search_path = pg_catalog, clinic_app, pg_temp AS $f$ "
    "SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid $f$; "
    "GRANT EXECUTE ON FUNCTION clinic_app.f() TO clinic_app; "
    "CREATE TRIGGER t BEFORE INSERT ON clinic_app.x FOR EACH ROW "
    "EXECUTE FUNCTION clinic_app.g(); "
    "SELECT set_config('app.current_user_id', %s, true), "
    "current_setting(%(name)s, true); PREPARE TRANSACTION 'x'"
)


def _planted_classes(language: str, text: str, found: Derived) -> set[str]:
    if language == "python":
        return {key for _, key, _ in source_violations(text, found)}
    return {key for key, _ in sql_violations(text, found)}


def test_the_boundary_is_declared_and_enforced(rbac_graph: RbacGraph) -> None:
    """The declared unhandled classes are exactly the ones the guard enforces:
    each has a planted example refused under its own key, the guard emits no
    undeclared key, and what the live code does is accepted. A class added
    without enforcement, or enforcement dropped, fails here by name."""
    settings = actor_settings(rbac_graph)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(_READER_OPERATOR)
        found = derived(settings)
        transaction.set_rollback(True)
    assert "@#@" in found.operators, "a reader operator is derived from the catalog"
    emitted = {
        key: _planted_classes(language, text, found) for key, language, text in _PLANTED
    }
    undeclared = set().union(*emitted.values()) - set(UNHANDLED)
    assert not undeclared, ("classes the guard emits but does not declare", undeclared)
    unenforced = [
        key
        for key in UNHANDLED
        if not any(
            key in _planted_classes(language, text, found)
            for planted, language, text in _PLANTED
            if planted == key
        )
    ]
    assert not unenforced, ("declared classes the guard does not refuse", unenforced)
    assert sql_violations(_CLEAN_SQL, found) == []
