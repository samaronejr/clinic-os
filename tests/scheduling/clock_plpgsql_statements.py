"""Closed statement dispatch from PostgreSQL 16 pl_gram.y's proc_stmt.

Only the forms below are understood. SQL fallback is deliberately restricted;
CALL/DO, dynamic execution, transaction control and unknown forms are refusals.
Expressions are still checked independently for explicit temporal coercions.
"""

from dataclasses import dataclass
from functools import lru_cache

from sqlparse import tokens

from .clock_tokens import sql_tokens

SQL_FORMS = frozenset({"select", "with", "insert", "update", "delete", "merge"})
SIMPLE_FORMS = SQL_FORMS | frozenset(
    {
        "perform",
        "raise",
        "assert",
        "exit",
        "continue",
        "return",
        "get",
        "open",
        "fetch",
        "move",
        "close",
        "null",
    }
)


@dataclass(frozen=True)
class Statement:
    kind: str
    parts: tuple[str, ...]


def boundary(parts: tuple[str, ...], start: int, stops: set[str]) -> int:
    depth = cases = 0
    for index in range(start, len(parts)):
        word = parts[index].lower()
        if depth == 0 and cases == 0 and word in stops:
            return index
        if word in {"(", "["}:
            depth += 1
        elif word in {")", "]"}:
            depth -= 1
        elif not depth and word == "case":
            cases += 1
        elif not depth and word == "end" and cases:
            cases -= 1
    return len(parts)


@lru_cache(maxsize=1024)
def pl_tokens(source: str) -> tuple[str, ...]:
    result: list[str] = []
    for kind, value in sql_tokens(source):
        if kind in tokens.Comment or kind in tokens.Whitespace:
            continue
        # sqlparse also speaks T-SQL: PostgreSQL's array subscripts must not
        # become SQL Server's square-bracket quoted identifiers.
        if kind in tokens.Name and value.startswith("[") and value.endswith("]"):
            result.extend(("[", *pl_tokens(value[1:-1]), "]"))
        else:
            result.extend(value.split() if kind in tokens.Keyword else (value,))
    return tuple(result)


@lru_cache(maxsize=512)
def statements(source: str) -> tuple[Statement, ...]:
    parts = pl_tokens(source)
    result = []
    index = 0
    declaring = False
    while index < len(parts):
        word = parts[index].lower()
        if word in {";", "begin", "declare", "else", "exception", "loop"}:
            if word in {"begin", "declare"}:
                declaring = word == "declare"
            index += 1
        elif word == "<<":
            stop = boundary(parts, index + 1, {">>"})
            index = stop + 1
        elif declaring:
            stop = boundary(parts, index, {";", "begin"})
            result.append(Statement("declaration", parts[index:stop]))
            index = stop
        elif word == "end":
            stop = boundary(parts, index, {";"})
            index = stop + 1
        elif word in {"if", "elsif", "when", "while", "for", "foreach", "case"}:
            stop_word = (
                "when"
                if word == "case"
                else "loop"
                if word in {"while", "for", "foreach"}
                else "then"
            )
            stop = boundary(parts, index + 1, {stop_word})
            result.append(Statement(word, parts[index:stop]))
            index = stop if word == "case" else stop + 1
        else:
            stop = boundary(parts, index, {";"})
            segment = parts[index:stop]
            assignment = boundary(segment, 0, {":=", "="})
            kind = (
                word
                if word in SIMPLE_FORMS
                else "assignment"
                if assignment < len(segment)
                else "unanalysed:" + word
            )
            result.append(Statement(kind, segment))
            index = stop + 1
    return tuple(result)
