"""Conservative PL/pgSQL name binding against live catalogs and declarations."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection
from sqlparse import tokens

from .clock_tokens import sql_tokens

if TYPE_CHECKING:
    from collections.abc import Mapping

# PL/pgSQL grammar options and implicit trigger parameters, not callable exemptions.
GRAMMAR = frozenset(
    {
        "raise",
        "notice",
        "exception",
        "perform",
        "loop",
        "elsif",
        "errcode",
        "message",
        "detail",
        "hint",
        "column",
        "constraint",
        "datatype",
        "table",
        "schema",
        "rowtype",
        "unique_violation",
        "foreign_key_violation",
        "check_violation",
        "not_null_violation",
        "exclusion_violation",
        "invalid_text_representation",
    }
)
TRIGGER_PARAMETERS = frozenset(
    {
        "new",
        "old",
        "tg_name",
        "tg_when",
        "tg_level",
        "tg_op",
        "tg_relid",
        "tg_relname",
        "tg_table_name",
        "tg_table_schema",
        "tg_nargs",
        "tg_argv",
    }
)


def identifier(value: str) -> str:
    return value[1:-1].replace('""', '"') if value.startswith('"') else value.lower()


def _is_name(kind: tokens._TokenType) -> bool:
    return (
        kind in tokens.Name
        or kind in tokens.Keyword
        or kind in tokens.Literal.String.Symbol
    )


def _factor_end(stream: list[tuple[tokens._TokenType, str]], start: int) -> int:
    index = start
    while index < len(stream) and stream[index][1].casefold() in {"lateral", "only"}:
        index += 1
    if index < len(stream) and stream[index][1] != "(":
        index += 1
        while index + 1 < len(stream) and stream[index][1] == ".":
            index += 2
    if index < len(stream) and stream[index][1] == "(":
        depth = 1
        index += 1
        while index < len(stream) and depth:
            depth += (stream[index][1] == "(") - (stream[index][1] == ")")
            index += 1
    return index


def _aliases(
    stream: list[tuple[tokens._TokenType, str]], reserved: set[str]
) -> set[str]:
    aliases = set()
    for index, (_, value) in enumerate(stream):
        if value.casefold() not in {"from", "join", "update"}:
            continue
        following = _factor_end(stream, index + 1)
        if following < len(stream) and stream[following][1].casefold() == "as":
            following += 1
        if following < len(stream):
            kind, alias = stream[following]
            if (
                _is_name(kind) or re.fullmatch(r"[a-zA-Z_][\w$]*", alias)
            ) and alias.casefold() not in reserved:
                aliases.add(identifier(alias))
    return aliases


def _declarations(
    stream: list[tuple[tokens._TokenType, str]], reserved: set[str]
) -> set[str]:
    declared: set[str] = set()
    declaring = False
    start = False
    for index, (kind, value) in enumerate(stream):
        word = value.casefold()
        if word == "declare":
            declaring = start = True
        elif word == "begin":
            declaring = start = False
        elif declaring and value == ";":
            start = True
        elif declaring and start and _is_name(kind):
            declared.add(identifier(value))
            start = False
        if not _is_name(kind):
            continue
        previous = stream[index - 1][1].casefold() if index else ""
        if previous in {"as", "for", "foreach", "<<"}:
            declared.add(identifier(value))
        if (
            previous == "("
            and index > 1
            and stream[index - 2][1].casefold() == "extract"
        ):
            declared.add(identifier(value))  # EXTRACT's grammar field, not an object.
    if any(value.casefold() == "conflict" for _, value in stream):
        declared.add("excluded")  # INSERT's implicit proposed row.
    return declared | _aliases(stream, reserved)


@dataclass(frozen=True)
class Bindings:
    functions: frozenset[int]
    relations: frozenset[int]
    unresolved: tuple[str, ...]
    types: frozenset[int]


class Identifiers:
    def __init__(
        self, functions: Mapping[str, set[int]], types: Mapping[str, set[int]]
    ) -> None:
        self.functions = functions
        self.types = types
        self.relations: dict[str, set[int]] = defaultdict(set)
        self.fields: dict[int, set[str]] = defaultdict(set)
        self.parameters: dict[int, set[str]] = defaultdict(set)
        self.triggers: dict[int, set[int]] = defaultdict(set)
        with connection.cursor() as cursor:
            cursor.execute("SELECT oid,relname FROM pg_class")
            for oid, name in cursor.fetchall():
                self.relations[str(name)].add(int(oid))
            cursor.execute(
                "SELECT attrelid,attname FROM pg_attribute WHERE NOT attisdropped"
            )
            for oid, name in cursor.fetchall():
                self.fields[int(oid)].add(str(name))
            cursor.execute("SELECT oid,proargnames,pronargs FROM pg_proc")
            for oid, names, count in cursor.fetchall():
                self.parameters[int(oid)].update(str(name) for name in (names or ()))
                self.parameters[int(oid)].update(
                    f"${index}" for index in range(1, int(count) + 1)
                )
            cursor.execute("SELECT tgfoid,tgrelid FROM pg_trigger")
            for function, relation in cursor.fetchall():
                self.triggers[int(function)].add(int(relation))
            cursor.execute("SELECT word FROM pg_get_keywords()")
            self.keywords = {str(row[0]) for row in cursor.fetchall()} | GRAMMAR
            cursor.execute("SELECT nspname FROM pg_namespace")
            self.schemas = {str(row[0]) for row in cursor.fetchall()}
            cursor.execute("SELECT word FROM pg_get_keywords() WHERE catcode='R'")
            self.reserved = {str(row[0]) for row in cursor.fetchall()}

    def bind(self, oid: int, source: str) -> Bindings:
        stream = [
            (kind, value)
            for kind, value in sql_tokens(source)
            if kind not in tokens.Comment and kind not in tokens.Whitespace
        ]
        names = [
            identifier(value)
            for index, (kind, value) in enumerate(stream)
            if _is_name(kind)
            and re.fullmatch(r'(?:"(?:[^"]|"")*"|[^\W\d][\w$]*|\$\d+)', value)
            and not (
                value.casefold() == "e"
                and index + 1 < len(stream)
                and stream[index + 1][0] in tokens.Literal.String.Single
            )
            and not (
                value.casefold() == "u"
                and index + 2 < len(stream)
                and stream[index + 1][1] == "&"
                and stream[index + 2][0] in tokens.Literal.String.Single
            )
        ]
        calls = {
            function for name in names for function in self.functions.get(name, ())
        }
        relations = {
            relation for name in names for relation in self.relations.get(name, ())
        }
        types = {type_oid for name in names for type_oid in self.types.get(name, ())}
        declared = _declarations(stream, self.reserved) | self.parameters[oid]
        for relation in relations | self.triggers[oid]:
            declared.update(self.fields[relation])
        # Named arguments/record outputs of resolved functions are declared names.
        for function in calls:
            declared.update(
                name for name in self.parameters[function] if not name.startswith("$")
            )
        if self.triggers[oid]:
            declared.update(TRIGGER_PARAMETERS)
        unknown = tuple(
            name
            for name in names
            if name not in self.functions
            and name not in self.relations
            and name not in declared
            and name not in self.types
            and name not in self.schemas
            and name not in self.keywords
        )
        return Bindings(
            frozenset(calls), frozenset(relations), unknown, frozenset(types)
        )
