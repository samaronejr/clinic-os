"""Derive every input the live permission decision reads, from the catalog.

The exemption probes certify "this function's outcome does not depend on
staff permission state". That claim is only as strong as the set of staff
states the probes vary, so the set of inputs is read from the migrated
database instead of being listed by hand: the body of every decision
function (``clinic_app.has_permission`` and whatever else the census derives
as a decision, plus the SQL the Python permission entry executes) and every
``clinic_app`` function those bodies reach, followed transitively.

Each body yields:
- the relations it reads (qualified or not, since ``search_path`` includes
  ``clinic_app``), with views expanded to their base relations through
  ``pg_depend``/``pg_rewrite``, and the columns of each relation whose name
  occurs in the body (an over-approximation: a column name that only
  appears in another relation's predicate still counts, which errs towards
  declaring more);
- every ``current_setting`` GUC it reads;
- every non-immutable ``pg_catalog`` function it calls (clocks, settings);
- the root's parameters (call inputs, chosen by the caller).
Row-level policies of a read relation count as more body text when the
function's owner does not bypass them.

It fails closed on what it cannot read: a reached function in a language
other than SQL/PL/pgSQL, dynamic SQL (``EXECUTE``), a ``current_setting``
without a literal name, a regclass cast or ``to_regclass`` lookup, and a
``SELECT *``/``alias.*`` projection (which reads every column: all columns
of the relation are counted).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from identity.permission_gate_census import CensusError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.backends.utils import CursorWrapper

_TOKEN: Final = re.compile(r"[a-z_][a-z0-9_$]*")
_LITERAL: Final = re.compile(r"'(?:[^']|'')*'")
_DOLLAR_BODY: Final = re.compile(r"\$([a-z_]*)\$.*?\$\1\$", re.DOTALL)
_COMMENT: Final = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_SETTING: Final = re.compile(r"current_setting\s*\(\s*('(?:[^']|'')*')?")
_STAR: Final = re.compile(r"(?:select|,)\s*(?:([a-z_][a-z0-9_]*)\.)?\*")
_OPAQUE: Final = (
    (re.compile(r"\bexecute\b"), "dynamic SQL (EXECUTE)"),
    (re.compile(r"::\s*regclass|\bto_regclass\s*\("), "a computed relation name"),
)


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    """Everything the decision functions can branch on."""

    functions: frozenset[str]
    columns: frozenset[str]
    settings: frozenset[str]
    catalog_calls: frozenset[str]
    parameters: frozenset[str]

    def keys(self) -> frozenset[str]:
        """One key per (reading function, input).

        ``column:<fn>:<relation>.<column>``, ``setting:<fn>:<guc>``,
        ``call:<fn>:<function>``, ``param:<fn>.<argument>``. Keys name the
        reading function, so a decision that starts reading an input some
        other function already reads is still a new key.
        """
        return frozenset(
            {f"column:{item}" for item in self.columns}
            | {f"setting:{item}" for item in self.settings}
            | {f"call:{item}" for item in self.catalog_calls}
            | {f"param:{item}" for item in self.parameters}
        )


@dataclass(frozen=True, slots=True)
class _Function:
    name: str
    language: str
    body: str
    owner_bypasses_rls: bool
    owner: str
    arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Relation:
    kind: str
    columns: frozenset[str]
    owner: str
    row_security: bool
    forced: bool
    policies: str


def _functions(cursor: CursorWrapper) -> dict[str, list[_Function]]:
    cursor.execute(
        "SELECT p.proname, l.lanname, p.prosrc, r.rolbypassrls, r.rolname, "
        "COALESCE(p.proargnames, ARRAY[]::text[]) "
        "FROM pg_catalog.pg_proc AS p "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
        "JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang "
        "JOIN pg_catalog.pg_roles AS r ON r.oid = p.proowner "
        "WHERE n.nspname = 'clinic_app'"
    )
    found: dict[str, list[_Function]] = {}
    for name, language, body, bypass, owner, arguments in cursor.fetchall():
        found.setdefault(str(name), []).append(
            _Function(
                str(name),
                str(language),
                str(body),
                bool(bypass),
                str(owner),
                tuple(str(argument) for argument in arguments),
            )
        )
    return found


def _relations(cursor: CursorWrapper) -> dict[str, _Relation]:
    cursor.execute(
        "SELECT c.relname, c.relkind, pg_catalog.pg_get_userbyid(c.relowner), "
        "c.relrowsecurity, c.relforcerowsecurity, "
        "COALESCE((SELECT pg_catalog.array_agg(a.attname::text) "
        "  FROM pg_catalog.pg_attribute AS a "
        "  WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), "
        "  ARRAY[]::text[]), "
        "COALESCE((SELECT pg_catalog.string_agg("
        "  COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), '') || ' ' || "
        "  COALESCE(pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), ''), ' ') "
        "  FROM pg_catalog.pg_policy AS p WHERE p.polrelid = c.oid), '') "
        "FROM pg_catalog.pg_class AS c "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'clinic_app' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
    )
    return {
        str(name): _Relation(
            str(kind),
            frozenset(str(column) for column in columns),
            str(owner),
            bool(security),
            bool(forced),
            str(policies),
        )
        for name, kind, owner, security, forced, columns, policies in (
            cursor.fetchall()
        )
    }


def _view_bases(cursor: CursorWrapper, view: str) -> set[str]:
    """Base relations a view reads, from its rewrite rule's dependencies."""
    cursor.execute(
        "SELECT DISTINCT d.refobjid::pg_catalog.regclass::text "
        "FROM pg_catalog.pg_rewrite AS rw "
        "JOIN pg_catalog.pg_depend AS d "
        "  ON d.classid = 'pg_catalog.pg_rewrite'::pg_catalog.regclass "
        " AND d.objid = rw.oid "
        "WHERE rw.ev_class = ('clinic_app.' || %s)::pg_catalog.regclass "
        "  AND d.refobjid <> rw.ev_class",
        [view],
    )
    return {str(name).removeprefix("clinic_app.") for (name,) in cursor.fetchall()}


def _catalog_readers(cursor: CursorWrapper) -> frozenset[str]:
    """``pg_catalog`` functions that are not immutable (clock, settings)."""
    cursor.execute(
        "SELECT DISTINCT p.proname FROM pg_catalog.pg_proc AS p "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'pg_catalog' AND p.provolatile <> 'i'"
    )
    return frozenset(str(name) for (name,) in cursor.fetchall())


def _code(text: str) -> str:
    """Lower-case body text without comments and string literals."""
    return _LITERAL.sub(" ", _COMMENT.sub(" ", text.lower()))


def _settings(text: str, where: str) -> set[str]:
    found: set[str] = set()
    for match in _SETTING.finditer(_COMMENT.sub(" ", text.lower())):
        literal = match.group(1)
        if literal is None:
            message = f"{where}: current_setting without a literal name"
            raise CensusError(message)
        found.add(literal[1:-1].replace("''", "'"))
    return found


def decision_inputs(cursor: CursorWrapper, roots: Iterable[str]) -> DecisionInputs:
    """Follow every root decision function and collect what it reads."""
    functions = _functions(cursor)
    relations = _relations(cursor)
    readers = _catalog_readers(cursor)
    roots = sorted(set(roots))
    missing = [name for name in roots if name not in functions]
    if missing:
        message = f"decision functions missing from the catalog: {missing}"
        raise CensusError(message)
    parameters = {
        f"{name}.{argument}"
        for name in roots
        for function in functions[name]
        for argument in function.arguments
        if argument
    }
    seen: set[str] = set()
    frontier = list(roots)
    found = _Found()
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        for function in functions[name]:
            frontier.extend(
                _read(cursor, relations, readers, set(functions), function, found)
            )
    return DecisionInputs(
        frozenset(seen),
        frozenset(found.columns),
        frozenset(found.settings),
        frozenset(found.calls),
        frozenset(parameters),
    )


@dataclass(slots=True)
class _Found:
    columns: set[str] = field(default_factory=set)
    settings: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)


def _read(  # noqa: PLR0913 - one body against the whole catalog
    cursor: CursorWrapper,
    relations: dict[str, _Relation],
    readers: frozenset[str],
    functions: set[str],
    function: _Function,
    found: _Found,
) -> list[str]:
    """Collect what one function body reads; return the functions it calls."""
    where = f"clinic_app.{function.name}"
    if function.language not in ("sql", "plpgsql"):
        message = f"{where} is opaque ({function.language})"
        raise CensusError(message)
    texts = [function.body]
    read: set[str] = set()
    called: list[str] = []
    pending = [function.body]
    while pending:
        text = pending.pop()
        code = _code(text)
        for pattern, what in _OPAQUE:
            if pattern.search(code):
                message = f"{where} uses {what}"
                raise CensusError(message)
        tokens = set(_TOKEN.findall(code))
        found.settings |= {f"{function.name}:{item}" for item in _settings(text, where)}
        found.calls |= {
            f"{function.name}:{item}"
            for item in (tokens & readers) - {"current_setting"}
        }
        called.extend(sorted(tokens & functions))
        for relation in sorted(tokens & set(relations)):
            fresh = _expand(cursor, relations, relation) - read
            read |= fresh
            pending.extend(_policies(relations, fresh, function))
            texts.extend(_policies(relations, fresh, function))
    words = set().union(*(set(_TOKEN.findall(_code(text))) for text in texts))
    starred = any(_STAR.search(_code(text)) for text in texts)
    for relation in read:
        names = relations[relation].columns
        chosen = names if starred else names & words
        found.columns |= {f"{function.name}:{relation}.{column}" for column in chosen}
    return called


def _policies(
    relations: dict[str, _Relation], read: set[str], function: _Function
) -> list[str]:
    """Row policies that apply to ``function`` on the relations it reads."""
    return [
        relations[name].policies
        for name in sorted(read)
        if relations[name].row_security
        and not function.owner_bypasses_rls
        and (relations[name].forced or relations[name].owner != function.owner)
        and relations[name].policies
    ]


def _expand(
    cursor: CursorWrapper, relations: dict[str, _Relation], name: str
) -> set[str]:
    """A table is itself; a view is (recursively) its base relations."""
    if relations[name].kind not in ("v", "m"):
        return {name}
    bases: set[str] = {name}
    for base in _view_bases(cursor, name):
        if base in relations:
            bases |= _expand(cursor, relations, base)
    return bases
