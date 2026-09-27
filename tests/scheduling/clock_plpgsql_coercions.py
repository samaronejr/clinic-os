"""Fail-closed temporal casts/assignments in PL/pgSQL, typed by PostgreSQL."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlparse import tokens

from .clock_dependencies import expression_tree
from .clock_identifiers import identifier
from .clock_tokens import sql_tokens

if TYPE_CHECKING:
    from .clock_coercions import Coercions
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


def _target_type(parts: list[str], variables: dict[str, int], types: Types) -> int:
    if len(parts) == 1:
        return variables.get(identifier(parts[0]), 0)
    if len(parts) == 3 and parts[1] == ".":
        row = variables.get(identifier(parts[0]), 0)
        relation = next(
            (relation for relation, oid in types.row_types.items() if oid == row), 0
        )
        return types.field_types.get((relation, identifier(parts[2])), 0)
    return 0


def _initialisers(
    parts: list[str], variables: dict[str, int], types: Types
) -> list[str]:
    result = []
    for index, value in enumerate(parts):
        if identifier(value) not in variables:
            continue
        target, end = _type_end(parts, index + 1, types)
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


def _implicit(
    parts: list[str], variables: dict[str, int], returns: int, types: Types
) -> list[str]:
    expressions = []
    for index, value in enumerate(parts):
        if value.casefold() == "return" and returns in types.temporal:
            target = returns
        elif value in {":=", "="}:
            start = index - 1
            if start > 1 and parts[start - 1] == ".":
                start -= 2
            target = _target_type(parts[start:index], variables, types)
        else:
            continue
        if target not in types.temporal:
            continue
        end = next(
            (
                position
                for position in range(index + 1, len(parts))
                if parts[position] == ";"
            ),
            len(parts),
        )
        expressions.append(
            "(" + _text(parts[index + 1 : end]) + ")::" + types.sql_names[target]
        )
    return expressions


class PLCoercions:
    def __init__(self, types: Types, coercions: Coercions) -> None:
        self.types = types
        self.coercions = coercions

    def count(self, oid: int, source: str, triggers: set[int]) -> int:
        parts = [
            value
            for kind, value in sql_tokens(source)
            if kind not in tokens.Whitespace and kind not in tokens.Comment
        ]
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
        for scope in scopes:
            for expression in expressions + _implicit(
                parts, scope, returns, self.types
            ):
                used = {identifier(value) for _, value in sql_tokens(expression)}
                arguments = {
                    name: self.types.sql_names[type_oid]
                    for name, type_oid in scope.items()
                    if name in used or any(value.startswith("$") for value in used)
                }
                tree = expression_tree(expression, arguments)
                if tree is None or self.coercions.count(tree):
                    unresolved.add(expression)
        return len(unresolved)
