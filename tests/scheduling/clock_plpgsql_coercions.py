"""Fail-closed temporal casts/assignments in PL/pgSQL, typed by PostgreSQL."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .clock_dependencies import expression_tree
from .clock_identifiers import identifier
from .clock_plpgsql_statements import pl_tokens, statements
from .clock_plpgsql_transfers import Transfers

if TYPE_CHECKING:
    from .clock_coercions import Coercions
    from .clock_plpgsql_statements import Statement
    from .clock_types import Types


def _text(parts: list[str]) -> str:
    return " ".join(parts)


def _pairs(parts: list[str]) -> dict[int, int]:
    stack = []
    pairs = {}
    for index, value in enumerate(parts):
        if value in {"(", "["}:
            stack.append(index)
        elif value in {")", "]"} and stack:
            start = stack.pop()
            pairs[start] = index
            pairs[index] = start
    return pairs


def _type_end(parts: list[str], start: int, types: Types) -> tuple[int, int]:
    found = (0, start)
    for end in range(start + 1, min(len(parts), start + 10) + 1):
        if parts[end - 1] in {";", ":=", "="}:
            break
        oid = types.resolve(_text(parts[start:end]))
        if oid:
            found = (oid, end)
    return found


def _declarations(parts: list[str], types: Types) -> dict[str, int]:
    result = {}
    declaring = False
    beginning = False
    for index, value in enumerate(parts):
        word = value.casefold()
        if word == "declare":
            declaring = beginning = True
        elif word == "begin":
            declaring = beginning = False
        elif declaring and value == ";":
            beginning = True
        elif declaring and beginning:
            start = index + 1
            if start < len(parts) and parts[start].casefold() == "constant":
                start += 1
            oid, _ = _type_end(parts, start, types)
            stop = next(
                (end for end in range(start, len(parts)) if parts[end] == ";"),
                len(parts),
            )
            if not oid and any(part.lower() == "cursor" for part in parts[start:stop]):
                oid = types.resolve("refcursor")
            if oid:
                result[identifier(value)] = oid
            beginning = False
    return result


def _operand_start(parts: list[str], end: int, pairs: dict[int, int]) -> int:
    start = pairs[end - 1] if parts[end - 1] in {")", "]"} else end - 1
    # Include a function/type constructor before its argument list.
    if (
        parts[start] == "("
        and start
        and re.fullmatch(r'"[^"]+"|[^\W\d][\w$]*', parts[start - 1])
        and parts[start - 1].casefold()
        not in {
            "return",
            "if",
            "then",
            "else",
            "select",
            "when",
            "or",
            "and",
            "not",
            "begin",
            "perform",
        }
    ):
        start -= 1
    while start > 1 and parts[start - 1] == ".":
        start -= 2
    if start and parts[start - 1] == "::":
        start = _operand_start(parts, start - 1, pairs)
    return start


def _single_operand(
    parts: list[str], start: int, end: int, pairs: dict[int, int]
) -> bool:
    index = start + 1
    while index < end:
        if parts[index] == ",":
            return False
        index = pairs[index] + 1 if parts[index] in {"(", "["} else index + 1
    return start + 1 < end


def _explicit(parts: list[str], types: Types, pairs: dict[int, int]) -> list[str]:
    expressions = []
    for index, value in enumerate(parts):
        if value == "::" and index:
            oid, end = _type_end(parts, index + 1, types)
            if oid in types.temporal:
                expressions.append(
                    _text(parts[_operand_start(parts, index, pairs) : end])
                )
        if value.casefold() == "cast" and index + 1 in pairs:
            end = pairs[index + 1]
            expressions.extend(
                _text(parts[index : end + 1])
                for position in range(index + 2, end)
                if parts[position].casefold() == "as"
                and types.resolve(_text(parts[position + 1 : end])) in types.temporal
            )
        if value == "(" and index and index in pairs:
            start = index - 1
            while start > 1 and parts[start - 1] == ".":
                start -= 2
            if types.resolve(
                _text(parts[start:index])
            ) in types.temporal and _single_operand(parts, index, pairs[index], pairs):
                expressions.append(_text(parts[start : pairs[index] + 1]))
    return expressions


def _initialisers(
    parts: list[str], variables: dict[str, int], types: Types
) -> list[str]:
    result = []
    for index, value in enumerate(parts):
        if identifier(value) not in variables:
            continue
        start = index + 1
        if start < len(parts) and parts[start].lower() == "constant":
            start += 1
        target, end = _type_end(parts, start, types)
        if target not in types.temporal:
            continue
        while end < len(parts) and parts[end].casefold() in {"not", "null"}:
            end += 1
        if end < len(parts) and parts[end].casefold() in {":=", "=", "default"}:
            stop = next(
                (pos for pos in range(end + 1, len(parts)) if parts[pos] == ";"),
                len(parts),
            )
            result.append(
                "(" + _text(parts[end + 1 : stop]) + ")::" + types.sql_names[target]
            )
    return result


class PLCoercions:
    def __init__(self, types: Types, coercions: Coercions) -> None:
        self.types = types
        self.coercions = coercions
        self.unanalysed: tuple[str, ...] = ()

    def declaration_risks(
        self,
        parsed: tuple[Statement, ...],
        variables: dict[str, int],
        parameters: dict[str, int],
    ) -> set[str]:
        unknown = set()
        seen = set(parameters)
        for statement in parsed:
            if statement.kind != "declaration":
                continue
            name = identifier(statement.parts[0])
            if name in seen or name not in variables:
                unknown.add("unanalysed-declaration:" + name)
            seen.add(name)
            declaration = list(statement.parts)
            if any(part.lower() == "cursor" for part in declaration):
                continue  # Cursor bindings have their own handler.
            start = (
                2
                if len(declaration) > 1 and declaration[1].lower() == "constant"
                else 1
            )
            _, end = _type_end(declaration, start, self.types)
            while end < len(declaration) and declaration[end].lower() in {
                "not",
                "null",
            }:
                end += 1
            if end < len(declaration) and declaration[end].lower() not in {
                ":=",
                "=",
                "default",
            }:
                unknown.add("unanalysed-declaration-form:" + name)
        return unknown

    def count(self, oid: int, source: str, triggers: set[int]) -> int:
        parts = list(pl_tokens(source))
        returns, parameters = self.types.prototypes[oid]
        variables = parameters | _declarations(parts, self.types)
        expressions = _explicit(parts, self.types, _pairs(parts)) + _initialisers(
            parts, variables, self.types
        )
        scopes = []
        for relation in sorted(triggers) or [0]:
            scope = dict(variables)
            if relation in self.types.row_types:
                scope.update(
                    new=self.types.row_types[relation],
                    old=self.types.row_types[relation],
                )
            scopes.append(scope)
        # Every binding of a multiply-attached trigger must prove the input type.
        unresolved = set()
        parsed = statements(source)
        unknown = self.declaration_risks(parsed, variables, parameters)
        for scope in scopes:
            transfers = Transfers(self.types, scope, returns, self.types.outputs[oid])
            for statement in parsed:
                transfers.inspect(statement)
            unknown.update(transfers.unknown)
            for expression in expressions + transfers.expressions:
                used = {identifier(value) for value in pl_tokens(expression)}
                arguments = {
                    name: self.types.sql_names[type_oid]
                    for name, type_oid in scope.items()
                    if name in used or any(value.startswith("$") for value in used)
                }
                tree = expression_tree(expression, arguments)
                if tree is None or self.coercions.count(tree):
                    unresolved.add(expression)
        self.unanalysed = tuple(sorted(unknown))
        return len(unresolved)
