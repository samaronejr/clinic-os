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

The classifier and the guard read text through one lexer
(``identity/sql_lexer.py``): a single left-to-right pass with PostgreSQL's
scanner rules that settles every quoted form before it looks for a comment.
What it cannot settle is the ``unlexable`` class, refused like the others;
the lexer's boundaries are checked against PostgreSQL itself
(``test_the_lexer_agrees_with_postgresql``).
"""

from __future__ import annotations

import ast
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from identity import actor_channels
from identity.sql_lexer import COMMENT, IDENTIFIER, STRING, lex
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from django.db.backends.utils import CursorWrapper

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
    "unlexable": (
        "text the lexer cannot settle: an unterminated quote, identifier, "
        "dollar-quoted string or block comment, or standard_conforming_strings "
        "named"
    ),
}
# Python text under apps/ the lexer cannot settle that is prose, never SQL
# (reviewed): (file, text) -> reason. Each entry must still exist and still
# be unsettled, so the list cannot go stale or grow silently.
_PROSE: Final[Mapping[tuple[str, str], str]] = {
    (
        "apps/ehr/management/commands/encrypt_attachment_objects.py",
        "Re-encrypt stored clinical attachment objects under each "
        "organization's tenant envelope. Requires the clinic_owner role.",
    ): "management command help text",
    ("apps/identity/showcase_data.py", "Today's schedule"): "showcase UI copy",
    (
        "apps/scheduling/appointment_forms.py",
        "Choose a window inside one of the physician's promised availability blocks.",
    ): "form validation message (UI copy)",
}

APPS: Final = Path(__file__).resolve().parents[2] / "apps"
# A Python value spliced into SQL text: unknown to the scanner.
_HOLE: Final = "\x00"
# The whole first argument: a literal, or a client placeholder (resolved by
# the observer from the bound value at run time).
_RESOLVABLE: Final = re.compile(
    r"\s*(?:'(?:[^']|'')*+'|%s|%\([^)]+\)s)\s*(?:::\s*[a-z_ .]+)?\s*[,)]",
    re.IGNORECASE,
)
_CREATE_OPERATOR: Final = re.compile(
    r"\bcreate\s+operator\s+(?!class\b|family\b)[^(]*\(([^)]*)\)", re.IGNORECASE
)
# A possibly qualified name; PostgreSQL allows space around the dot.
_OPERATOR_FUNCTION: Final = re.compile(
    r'\b(?:function|procedure)\s*=\s*([\w"$]+(?:\s*\.\s*[\w"$]+)*)', re.IGNORECASE
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
    """What one SQL-bearing text does that the classifier cannot fully see,
    read through the classifier's own lexer."""
    lexed = lex(text)
    code, bare = lexed.code, lexed.bare
    return [
        *(("unlexable", reason) for reason in lexed.unsettled),
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
        name = (
            function.group(1).split(".")[-1].strip().strip('"').lower()
            if function
            else ""
        )
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


def _parts(node: ast.AST) -> list[ast.AST]:
    """The pieces ``_folded`` joins into ``node``'s text: scanned only as
    part of the whole, never alone (a piece of a statement is not one)."""
    if isinstance(node, ast.JoinedStr):
        return list(node.values)
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add)
        and _folded(node) is not None
    ):
        return [node.left, node.right]
    return []


def folded_texts(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """(line, text) of every maximal string expression, docstrings aside."""
    skipped = _docstrings(tree) | {
        id(part) for node in ast.walk(tree) for part in _parts(node)
    }
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        text = _folded(node)
        if text is not None:
            yield getattr(node, "lineno", 0), text


def source_violations(
    source: str, found: Derived, prose: frozenset[str] = frozenset()
) -> list[tuple[int, str, str]]:
    """(line, class, detail) for every refused construct in one source file;
    a reviewed ``prose`` text is not SQL, so only its lexing is excused."""
    tree = ast.parse(source)
    refused = set(python_violations(tree))
    for line, text in folded_texts(tree):
        refused.update(
            (line, key, detail)
            for key, detail in sql_violations(text, found)
            if not (key == "unlexable" and text in prose)
        )
    return sorted(refused)


def scan(root: Path, found: Derived) -> dict[str, list[tuple[int, str, str]]]:
    refused: dict[str, list[tuple[int, str, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        name = str(path.relative_to(root.parent))
        prose = frozenset(text for file, text in _PROSE if file == name)
        if found_here := source_violations(path.read_text(), found, prose):
            refused[name] = found_here
    return refused


def test_the_prose_allowlist_is_exact() -> None:
    """Every excused text still exists where it is listed and still cannot be
    lexed; nothing else under apps/ is unsettled."""
    unsettled = {
        (str(path.relative_to(APPS.parent)), text)
        for path in sorted(APPS.rglob("*.py"))
        for _, text in folded_texts(ast.parse(path.read_text()))
        if lex(text).unsettled
    }
    assert unsettled == set(_PROSE)


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


# At least one planted example per declared class, written with the guard:
# (class, language, text).
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
    ("unlexable", "sql", "SELECT 'x"),
    ("unlexable", "sql", "SELECT E'x\\'"),
    ("unlexable", "sql", 'SELECT "x'),
    ("unlexable", "sql", "SELECT $f$ x"),
    ("unlexable", "sql", "SELECT 1 /* /* */"),
    # A dollar-quoted body is lexed as SQL (function bodies, DO blocks): one
    # that is not SQL cannot be settled.
    ("unlexable", "sql", "SELECT $$/*$$"),
    ("unlexable", "sql", "SELECT $$it's$$"),
    ("unlexable", "sql", "SET standard_conforming_strings = off"),
    (
        "unlexable",
        "sql",
        "SELECT set_config('standard_conforming_strings', 'off', false)",
    ),
)

# R9-1, the round-9 gate review's shapes: a literal or escape string that
# holds a comment marker or a quote, ahead of what the guard must see.
R9_S4_DDL: Final = (
    "CREATE FUNCTION clinic_app.zz_r9_s4(p text) RETURNS boolean "
    "LANGUAGE plpgsql AS $f$\n"
    "DECLARE r boolean;\n"
    "BEGIN\n"
    "  IF '--' <> '' THEN EXECUTE 'SELECT pg_catalog.current_' || "
    "'setting(''app.current_' || 'user_id'', true) = $1' INTO r USING p; "
    "END IF;\n"
    "  RETURN r;\n"
    "END $f$"
)
R9_S5_DDL: Final = (
    "CREATE FUNCTION clinic_app.zz_r9_s5(p text) RETURNS boolean "
    "LANGUAGE plpgsql AS $f$\n"
    "DECLARE q text := E'\\''; r boolean;\n"
    "BEGIN EXECUTE 'SELECT pg_catalog.current_' || "
    "'setting(''app.current_' || 'user_id'', true) = $1' INTO r USING p; "
    "RETURN r; END $f$"
)
# Shapes nobody wrote against the guard's patterns, each with its source:
# (class, language, text, source). "r9" is the round-9 gate review, "r8"/"r7"
# earlier reviews, "pg16 docs" an example printed in the PostgreSQL 16
# manual (the page is named).
_INDEPENDENT: Final = (
    ("dynamic-execute", "sql", R9_S4_DDL, "r9 E02"),
    ("dynamic-execute", "sql", R9_S5_DDL, "r9 E03"),
    ("pg-temp-name", "sql", "SELECT '--', pg_temp.zz9(%s)", "r9 E04"),
    (
        "text-executor",
        "sql",
        "SELECT '--', pg_catalog.query_to_xml(%s, false, false, '')",
        "r9 E05",
    ),
    (
        "server-prepare",
        "sql",
        "SELECT '--' AS a; PREPARE zz9(text) AS SELECT 1",
        "r9 E07",
    ),
    (
        "operator-alias",
        "sql",
        "CREATE OPERATOR clinic_app.@@@ "
        "(RIGHTARG = text, FUNCTION = pg_catalog . current_setting)",
        "r9 note",
    ),
    ("reader-operator", "sql", "SELECT (OPERATOR(clinic_app.@@@) %s) = %s", "r8 A1"),
    (
        "unicode-escape",
        "sql",
        'SELECT (OPERATOR(U&"clinic\\005Fapp".@@@) %s) = %s',
        "r9 v4",
    ),
    (
        "pg-temp-search-path",
        "sql",
        "SET search_path = pg_temp, clinic_app, public",
        "r9 v3",
    ),
    (
        "temporary-object",
        "sql",
        "CREATE TEMP VIEW intake_clinicintakepolicy AS "
        "SELECT * FROM clinic_app.intake_clinicintakepolicy",
        "r9 v3b",
    ),
    (
        "libpq-direct",
        "python",
        "result = connection.connection.pgconn.exec_(\n"
        "    b\"SELECT pg_catalog.current_setting('app.current_user_id', true)\"\n"
        ")\n",
        "r7 1e",
    ),
    (
        "server-prepare",
        "sql",
        "PREPARE fooplan (int, text, bool, numeric) AS\n"
        "    INSERT INTO foo VALUES($1, $2, $3, $4);",
        "pg16 docs sql-prepare",
    ),
    (
        "dynamic-execute",
        "sql",
        "EXECUTE fooplan(1, 'Hunter Valley', 't', 200.00);",
        "pg16 docs sql-prepare",
    ),
    (
        "dynamic-execute",
        "sql",
        "EXECUTE 'SELECT count(*) FROM mytable WHERE inserted_by = $1 AND "
        "inserted <= $2'\n   INTO c\n   USING checked_user, checked_date;",
        "pg16 docs plpgsql-statements",
    ),
    (
        "temporary-object",
        "sql",
        "CREATE TEMP TABLE films_recent ON COMMIT DROP AS\n"
        "  EXECUTE recentfilms('2002-01-01');",
        "pg16 docs sql-createtableas",
    ),
    ("unicode-escape", "sql", "SELECT U&'d\\0061t\\+000061';", "pg16 docs sql-syntax"),
    (
        "unicode-escape",
        "sql",
        "SELECT U&\"d!0061t!+000061\" UESCAPE '!';",
        "pg16 docs sql-syntax",
    ),
    ("enumerating-reader", "sql", "SHOW ALL;", "pg16 docs sql-show"),
    (
        "unresolvable-name",
        "sql",
        "CREATE FUNCTION clinic_app.zz_varfn(k text) RETURNS text "
        "LANGUAGE plpgsql AS $f$ BEGIN "
        "RETURN pg_catalog.current_setting(k, true); END $f$",
        "r8 zz_varfn (the review's plpgsql body reading via a variable)",
    ),
    (
        "raw-socket",
        "python",
        "os.write(connection.connection.fileno(), frame)\n",
        "r8 raw-socket writing (the review's boundary shape)",
    ),
    (
        "raw-socket",
        "python",
        "sock = socket.fromfd(\n"
        "    connection.connection.fileno(), socket.AF_INET, socket.SOCK_STREAM\n"
        ")\n",
        "python 3.12 docs socket.fromfd",
    ),
    (
        "unlexable",
        "sql",
        "SET standard_conforming_strings = on;",
        "pg_dump 16.14 output header",
    ),
)
# Text PostgreSQL reads as a literal, an identifier or a comment, put ahead
# of a shape: the guard must refuse the shape all the same (R9-1).
_DESYNC: Final = (
    "'--', ",
    "'/*', ",
    "'*/' || '--', ",
    "E'\\'', ",
    "E'\\\\', ",
    "$x$--$x$, ",
    "$x$/*$x$, ",
    "/* /* */ */ ",
    '"a--b", ',
    "'con'\n'--', ",
    "E'a\\''\n'\\'--', ",
)
# The same R9 shapes as a module under apps/ would hold them (the review
# planted them in apps/tenancy/ and its migrations).
_R9_MODULE: Final = f"""
from django.db import connection, migrations


def s4():
    return migrations.RunSQL({R9_S4_DDL!r})


def s5():
    return migrations.RunSQL({R9_S5_DDL!r})


def e04(value):
    with connection.cursor() as cursor:
        cursor.execute("SELECT '--', pg_temp.zz9(%s)", [value])


def e05(value):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT '--', pg_catalog.query_to_xml(%s, false, false, '')", [value]
        )


def e07():
    with connection.cursor() as cursor:
        cursor.execute("SELECT '--' AS a; PREPARE zz9(text) AS SELECT 1")
"""
_R9_MODULE_CLASSES: Final = {
    "dynamic-execute",
    "pg-temp-name",
    "text-executor",
    "server-prepare",
}
# Reader operators, derived while they exist (the live catalog has none).
_READER_OPERATORS: Final = (
    "CREATE OPERATOR clinic_app.@#@ "
    "(RIGHTARG = text, FUNCTION = pg_catalog.current_setting)",
    "CREATE OPERATOR clinic_app.@@@ "
    "(RIGHTARG = text, FUNCTION = pg_catalog.current_setting)",
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


def _found_with_reader_operators(rbac_graph: RbacGraph) -> Derived:
    settings = actor_settings(rbac_graph)
    with transaction.atomic():
        with connection.cursor() as cursor:
            for statement in _READER_OPERATORS:
                cursor.execute(statement)
        found = derived(settings)
        transaction.set_rollback(True)
    assert {"@#@", "@@@"} <= found.operators, "reader operators are derived"
    return found


def test_the_boundary_is_declared_and_enforced(rbac_graph: RbacGraph) -> None:
    """The declared unhandled classes are exactly the ones the guard enforces:
    each has a planted example refused under its own key, the guard emits no
    undeclared key, and what the live code does is accepted. A class added
    without enforcement, or enforcement dropped, fails here by name. The
    shapes written elsewhere (reviews, the PostgreSQL manual) are refused
    under their class too, so the pin is not only the guard's own spelling."""
    found = _found_with_reader_operators(rbac_graph)
    emitted = {
        key: _planted_classes(language, text, found) for key, language, text in _PLANTED
    }
    independent = {
        (key, source, text[:60]): _planted_classes(language, text, found)
        for key, language, text, source in _INDEPENDENT
    }
    undeclared = set().union(*emitted.values(), *independent.values()) - set(UNHANDLED)
    unenforced = [
        key
        for key in UNHANDLED
        if not any(
            key in _planted_classes(language, text, found)
            for planted, language, text in _PLANTED
            if planted == key
        )
    ]
    missed = [
        shape for shape, classes in independent.items() if shape[0] not in classes
    ]
    # One assertion, so a regression reports every class and shape it breaks.
    problems = {
        reason: found_here
        for reason, found_here in (
            ("classes the guard emits but does not declare", undeclared),
            ("declared classes the guard does not refuse", unenforced),
            ("independent shapes the guard does not refuse", missed),
        )
        if found_here
    }
    assert not problems, problems
    assert sql_violations(_CLEAN_SQL, found) == []


def test_desynchronising_text_hides_no_planted_shape(rbac_graph: RbacGraph) -> None:
    """R9-1: a literal holding ``--`` or ``/*``, an escape string holding a
    quote, a dollar-quoted comment marker, a nested comment, a quoted
    identifier or a continued literal, put ahead of any planted SQL shape,
    leaves the shape refused under its class."""
    found = _found_with_reader_operators(rbac_graph)
    shapes = [
        (key, text)
        for key, language, text, *_ in (*_PLANTED, *_INDEPENDENT)
        if language == "sql"
    ]
    hidden = [
        (key, prefix, text[:60])
        for key, text in shapes
        for prefix in _DESYNC
        if key not in _planted_classes("sql", prefix + text, found)
    ]
    assert not hidden, ("shapes a desynchronising prefix hides", hidden)
    module = source_violations(_R9_MODULE, found)
    assert {key for _, key, _ in module} == _R9_MODULE_CLASSES, module


# Pieces for fragments PostgreSQL and the lexer must segment alike: string
# literals of every form (each holding a comment marker, a quote or an
# escape), quoted identifiers, and separators holding comments.
_ORACLE_LITERALS: Final = (
    "'--'",
    "'/*'",
    "'*/'",
    "'it''s'",
    "'a\\'",
    "E'\\''",
    "E'--\\\\'",
    "E'x\\'--'",
    "E'\\\\'",
    "e'/*\\''",
    "$x$--$x$",
    "$$*/$$",
    "$q$ '--' $q$",
    "$a$ /* $b$ */ $a$",
    "U&'d\\0061t\\+000061'",
    "U&'--'",
    "B'0101'",
    "X'1F'",
    "N'n--'",
    "'con'\n'--tinued'",
    "E'a\\''\n'\\'b'",
    "'x' -- c\n'y'",
)
_ORACLE_IDENTIFIERS: Final = (
    '"a--b"',
    '"x/*y"',
    '"q""q"',
    '"$1"',
    'U&"d\\0061t\\+000061"',
    'U&"--"',
)
_ORACLE_SEPARATORS: Final = (
    ", ",
    " /* c */ , ",
    " -- c ' \" $$\n, ",
    " /* /* n */ '*/ , ",
    ",\n",
    "/**/,",
)
_ORACLE_FRAGMENTS: Final = 400


def _oracle_fragments() -> list[str]:
    chooser = random.Random(17)  # noqa: S311 - deterministic test fragments
    fragments = []
    for _ in range(_ORACLE_FRAGMENTS):
        items = [
            f"{chooser.choice(_ORACLE_LITERALS)} AS "
            f"{chooser.choice(_ORACLE_IDENTIFIERS)}"
            for _ in range(chooser.randint(1, 4))
        ]
        text = items[0]
        for item in items[1:]:
            text += chooser.choice(_ORACLE_SEPARATORS) + item
        fragments.append("SELECT " + text)
    return fragments


def _run(
    cursor: CursorWrapper, text: str
) -> tuple[tuple[object, ...], tuple[str, ...]]:
    cursor.execute(text)
    row = cursor.fetchone()
    assert row is not None, text
    return tuple(row), tuple(column.name for column in cursor.description)


def test_the_lexer_agrees_with_postgresql() -> None:
    """A differential oracle: for generated fragments, the lexer's literal,
    identifier and comment spans are the ones PostgreSQL reads. Each literal
    span alone yields the column's value, each identifier span alone the
    column's name, and blanking every comment span changes nothing."""
    fragments = _oracle_fragments()
    checked = {STRING: 0, IDENTIFIER: 0, COMMENT: 0}
    with connection.cursor() as cursor:
        cursor.execute("SHOW standard_conforming_strings")
        assert cursor.fetchone() == ("on",)
        for text in fragments:
            lexed = lex(text)
            assert not lexed.unsettled, (text, lexed.unsettled)
            row, names = _run(cursor, text)
            strings = [s for s in lexed.spans if s.kind in (STRING, "dollar")]
            identifiers = [s for s in lexed.spans if s.kind == IDENTIFIER]
            comments = [s for s in lexed.spans if s.kind == COMMENT]
            values = tuple(
                _run(cursor, f"SELECT {text[s.start : s.end]}")[0][0] for s in strings
            )
            labels = tuple(
                _run(cursor, f"SELECT 1 AS {text[s.start : s.end]}")[1][0]
                for s in identifiers
            )
            assert (values, labels) == (row, names), text
            uncommented = list(text)
            for span in comments:
                for index in range(span.start, span.end):
                    if uncommented[index] not in "\n\r":
                        uncommented[index] = " "
            assert _run(cursor, "".join(uncommented)) == (row, names), text
            checked[STRING] += len(strings)
            checked[IDENTIFIER] += len(identifiers)
            checked[COMMENT] += len(comments)
    print("ORACLE", len(fragments), "fragments", checked)  # noqa: T201 - receipt
    assert all(checked.values())
