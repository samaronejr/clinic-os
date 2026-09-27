"""Inspect PostgreSQL's analysed expression trees for temporal coercions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .clock_types import Types

LEXEMES = re.compile(r'"(?:\\.|[^"\\])*"|[{}()\[\]]|[^\s{}()\[\]]+')


@dataclass
class Node:
    kind: str
    values: dict[str, str] = field(default_factory=dict)
    children: dict[str, list[Node]] = field(default_factory=dict)

    def walk(self) -> Iterator[Node]:
        yield self
        for group in self.children.values():
            for child in group:
                yield from child.walk()

    def result_type(self) -> int:
        for key in (
            "resulttype",
            "funcresulttype",
            "opresulttype",
            "vartype",
            "paramtype",
            "consttype",
            "coalescetype",
            "casetype",
            "array_typeid",
            "typeId",
            "type",
        ):
            if key in self.values:
                return int(self.values[key])
        return 0


class Tree:
    def __init__(self, source: str) -> None:
        self.tokens = LEXEMES.findall(source)
        self.index = 0

    def group(self, end: str = "") -> list[Node]:
        nodes = []
        while self.index < len(self.tokens):
            part = self.tokens[self.index]
            self.index += 1
            if part == end:
                break
            if part == "{":
                nodes.append(self.node())
            elif part in {"(", "["}:
                nodes.extend(self.group(")" if part == "(" else "]"))
        return nodes

    def node(self) -> Node:
        node = Node(self.tokens[self.index])
        self.index += 1
        key = ""
        while self.index < len(self.tokens):
            part = self.tokens[self.index]
            self.index += 1
            if part == "}":
                return node
            if part.startswith(":"):
                key = part[1:]
            elif part == "{":
                node.children.setdefault(key, []).append(self.node())
            elif part in {"(", "["}:
                node.children.setdefault(key, []).extend(
                    self.group(")" if part == "(" else "]")
                )
            elif key:
                node.values[key] = part
                key = ""
        message = "unterminated PostgreSQL expression tree"
        raise AssertionError(message)


@lru_cache(maxsize=512)
def nodes(source: str) -> tuple[Node, ...]:
    return tuple(node for root in Tree(source).group() for node in root.walk())


class Coercions:
    def __init__(self, types: Types) -> None:
        self.types = types
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT castfunc FROM pg_cast WHERE casttarget=ANY(%s) AND castfunc<>0",
                [sorted(types.temporal)],
            )
            self.functions = {int(row[0]) for row in cursor.fetchall()}

    def count(self, source: str) -> int:
        return sum(self.unresolved(node) for node in nodes(source))

    def unresolved(self, node: Node) -> bool:
        if node.result_type() not in self.types.temporal:
            return False
        if node.kind in {
            "COERCEVIAIO",
            "COERCETODOMAIN",
            "ARRAYCOERCEEXPR",
            "RELABELTYPE",
        }:
            operands = node.children.get("arg", [])
        elif node.kind == "FUNCEXPR" and (
            int(node.values.get("funcid", "0")) in self.functions
            or node.values.get("funcformat") in {"1", "2"}
        ):
            operands = node.children.get("args", [])[:1]
        else:
            return False
        return not operands or any(
            operand.kind != "CONST"
            and operand.result_type() not in self.types.nontext_temporal
            for operand in operands
        )
