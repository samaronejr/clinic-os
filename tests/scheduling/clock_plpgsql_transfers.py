"""Account for every target-writing PL/pgSQL statement, or refuse it."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

from .clock_identifiers import identifier
from .clock_plpgsql_statements import SQL_FORMS, Statement, boundary

if TYPE_CHECKING:
    from .clock_types import Types


def target_type(parts: tuple[str, ...], variables: dict[str, int], types: Types) -> int:
    if not parts:
        return 0
    oid = variables.get(identifier(parts[0]), 0)
    index = 1
    while index < len(parts):
        if parts[index] == "." and index + 1 < len(parts):
            relation = next(
                (rel for rel, row in types.row_types.items() if row == oid), 0
            )
            if (
                relation
                and (relation, identifier(parts[index + 1])) not in types.field_types
            ):
                # A fixed row without this field raises undefined_column; unlike
                # an untyped RECORD it cannot perform a hidden I/O assignment.
                return -1
            oid = types.field_types.get((relation, identifier(parts[index + 1])), 0)
            index += 2
        elif parts[index] == "[":
            oid = next(iter(types.wrappers.get(oid, ())), 0)
            index = boundary(parts, index + 1, {"]"}) + 1
        else:
            return 0
    return oid


def targets(parts: tuple[str, ...]) -> list[tuple[str, ...]]:
    result = []
    index = 0
    while index < len(parts):
        end = boundary(parts, index, {","})
        result.append(parts[index:end])
        index = end + 1
    return result


def query_without_into(
    parts: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    # pl_gram.y recognises INTO even inside parentheses. Inspect every token,
    # excluding only the grammar's INSERT/MERGE object-target exceptions.
    depth = 0
    positions = []
    for index, part in enumerate(parts):
        if part in {"(", "["}:
            depth += 1
        elif part in {")", "]"}:
            depth -= 1
        elif part.lower() == "into" and (
            not index or parts[index - 1].lower() not in {"insert", "merge"}
        ):
            positions.append((index, depth))
    if not positions:
        return parts, (), False
    if len(positions) != 1 or positions[0][1] != 0:
        return parts, (), True
    at = positions[0][0]
    begin = at + 1
    if begin < len(parts) and parts[begin].lower() == "strict":
        begin += 1
    end = boundary(
        parts,
        begin,
        {
            "from",
            "where",
            "group",
            "having",
            "order",
            "limit",
            "offset",
            "for",
            "union",
            "returning",
        },
    )
    return parts[:at] + parts[end:], parts[begin:end], False


def query_expression(query: tuple[str, ...], oid: int, types: Types) -> str | None:
    # A SELECT * from the same named row type cannot coerce any field. Its
    # predicates are not assignment operands (explicit casts remain checked).
    lower = tuple(part.lower() for part in query)
    if lower[:3] == ("select", "*", "from"):
        end = boundary(query, 3, {"where", "order", "limit", "for"})
        if types.resolve(" ".join(query[3:end]) + "%ROWTYPE") == oid:
            return None  # Identical named row type: no I/O coercion to analyse.
    text = " ".join(query)
    if oid in types.row_types.values():
        # This is program text sent only to the rolled-back analyser, not a
        # LiteralString SQL template or an interpolated query-data value.
        program = StringIO()
        program.writelines(("SELECT row_value FROM (", text, ") AS row_value"))
        text = program.getvalue()
    return "(" + text + ")::" + types.sql_names[oid]


class Transfers:
    def __init__(
        self,
        types: Types,
        variables: dict[str, int],
        returns: int,
        outputs: tuple[int, ...],
    ) -> None:
        self.types = types
        self.variables = variables
        self.returns = returns
        self.outputs = outputs
        self.expressions: list[str] = []
        self.unknown: list[str] = []
        self.cursors: dict[str, list[tuple[str, ...]]] = {}

    def query(self, query: tuple[str, ...], destination: tuple[str, ...]) -> None:
        target_list = targets(destination)
        oids = [
            target_type(target, self.variables, self.types) for target in target_list
        ]
        if not oids or 0 in oids:
            self.unknown.append("unresolved-target")
        elif any(oid in self.types.temporal for oid in oids):
            if len(oids) != 1 or not query or query[0].lower() != "select":
                self.unknown.append("unanalysed-query-target-list")
            else:
                self.project(query, oids[0])

    def project(self, query: tuple[str, ...], oid: int) -> None:
        expression = query_expression(query, oid, self.types)
        if expression is not None:
            self.expressions.append(expression)

    def cursor(self, statement: Statement) -> None:
        parts = statement.parts
        if statement.kind in {"open", "declaration"}:
            at = boundary(parts, 0, {"for"})
            name = identifier(parts[1 if statement.kind == "open" else 0])
            if at < len(parts) and parts[at + 1].lower() == "select":
                self.cursors.setdefault(name, []).append(parts[at + 1 :])
            else:
                self.unknown.append("unanalysed-cursor")
        else:
            at = boundary(parts, 0, {"into"})
            name = identifier(parts[at - 1]) if at < len(parts) else ""
            if name not in self.cursors:
                self.unknown.append("unanalysed-fetch")
            for query in self.cursors.get(name, ()):
                self.query(query, parts[at + 1 :])

    def loop(self, statement: Statement) -> None:
        parts = statement.parts
        at = boundary(parts, 1, {"in"})
        query = parts[at + 1 :]
        target = parts[1:at]
        if statement.kind == "foreach":
            sliced = boundary(target, 0, {"slice"})
            slice_count = target[sliced + 1 :] if sliced < len(target) else ()
            target = target[:sliced]
            if not query or query[0].lower() != "array":
                self.unknown.append("unanalysed-foreach")
                return
            array = " ".join(query[1:])
            value = (
                array
                if slice_count and slice_count != ("0",)
                else "unnest(" + array + ")"
            )
            query = ("SELECT", value)
        elif ".." in query:
            # Integer FOR shadows its target with a new integer variable.
            name = identifier(target[0])
            integer = self.types.resolve("integer")
            if name in self.variables and self.variables[name] != integer:
                self.unknown.append("unanalysed-loop-shadow")
            self.variables[name] = integer
            return
        elif not query or query[0].lower() not in {"select", "with"}:
            name = identifier(query[0]) if query else ""
            if name not in self.cursors:
                self.unknown.append("unanalysed-for-source")
            for cursor_query in self.cursors.get(name, ()):
                self.query(cursor_query, target)
            return
        self.query(query, target)

    def returning(self, parts: tuple[str, ...]) -> None:
        expression = parts[1:]
        if not expression:
            return  # OUT variables already have their declared types.
        if expression[0].lower() == "next":
            expression = expression[1:]
        if not expression:
            return
        if expression[0].lower() == "query":
            if len(expression) < 2 or expression[1].lower() not in {"select", "with"}:
                self.unknown.append("unanalysed-return-query")
            elif any(oid in self.types.temporal for oid in self.outputs):
                self.unknown.append("unanalysed-return-query-out")
            elif self.returns in self.types.temporal:
                self.project(expression[1:], self.returns)
        elif self.returns in self.types.temporal:
            self.expressions.append(
                "(" + " ".join(expression) + ")::" + self.types.sql_names[self.returns]
            )

    def inspect(self, statement: Statement) -> None:
        parts = statement.parts
        kind = statement.kind
        if kind.startswith("unanalysed:"):
            self.unknown.append(kind)
        elif kind in SQL_FORMS:
            self.sql_statement(parts)
        elif kind in {"for", "foreach"}:
            self.loop(statement)
        elif kind in {"open", "fetch"}:
            self.cursor(statement)
        elif kind == "return":
            self.returning(parts)
        elif kind == "get":
            self.diagnostics(parts)
        elif kind == "assignment":
            self.assignment(parts)
        elif kind == "declaration" and any(p.lower() == "cursor" for p in parts):
            self.cursor(statement)

    def sql_statement(self, parts: tuple[str, ...]) -> None:
        query, destination, unanalysed = query_without_into(parts)
        if unanalysed:
            self.unknown.append("unanalysed-into-placement")
        elif destination:
            self.query(query, destination)

    def assignment(self, parts: tuple[str, ...]) -> None:
        at = boundary(parts, 0, {":=", "="})
        oid = target_type(parts[:at], self.variables, self.types)
        if not oid:
            self.unknown.append("unresolved-assignment-target")
        elif oid in self.types.temporal:
            self.expressions.append(
                "(" + " ".join(parts[at + 1 :]) + ")::" + self.types.sql_names[oid]
            )

    def diagnostics(self, parts: tuple[str, ...]) -> None:
        at = boundary(parts, 1, {"diagnostics"})
        for item in targets(parts[at + 1 :]):
            split = boundary(item, 0, {"=", ":="})
            oid = target_type(item[:split], self.variables, self.types)
            if not oid or oid in self.types.temporal:
                # Diagnostics are text or integer, never non-text temporal.
                self.unknown.append("diagnostics-target")
