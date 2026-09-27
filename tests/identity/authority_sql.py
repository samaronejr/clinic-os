"""Conservative SQL references; unresolved executable text is never an exemption."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from sqlparse import lexer, tokens

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

Token = tuple[object, str]
_tokenize = cast("Callable[[str], Iterable[Token]]", lexer.tokenize)
SYNTAX_CALLS = frozenset(
    {
        "all",
        "and",
        "any",
        "array",
        "as",
        "case",
        "cast",
        "check",
        "coalesce",
        "exists",
        "extract",
        "filter",
        "if",
        "in",
        "not",
        "nullif",
        "only",
        "operator",
        "or",
        "over",
        "return",
        "row",
        "select",
        "some",
        "values",
        "when",
        "with",
    }
)
DYNAMIC = frozenset({"execute", "prepare", "do", "call"})
DDL = frozenset({"create", "alter", "drop", "grant", "revoke", "copy"})


@dataclass
class SqlReferences:
    names: set[tuple[str, ...]] = field(default_factory=set)
    calls: set[tuple[str, ...]] = field(default_factory=set)
    operators: set[str] = field(default_factory=set)
    settings: set[str] = field(default_factory=set)
    opaque: set[str] = field(default_factory=set)
    writes: bool = False


def _identifier(kind: object, value: str) -> bool:
    return (
        kind in tokens.Name
        or kind in tokens.Keyword
        or kind is tokens.Literal.String.Symbol
    ) and value not in {"%s", "%b", "%t"}


def _name(value: str) -> str:
    return value[1:-1].replace('""', '"') if value.startswith('"') else value.lower()


def _qualified(items: Sequence[Token]) -> Iterator[tuple[tuple[str, ...], int, bool]]:
    index = 0
    while index < len(items):
        kind, value = items[index]
        if not _identifier(kind, value):
            index += 1
            continue
        quoted = value.startswith('"')
        parts = [_name(value)]
        end = index + 1
        while (
            end + 1 < len(items)
            and items[end][1] == "."
            and _identifier(*items[end + 1])
        ):
            parts.append(_name(items[end + 1][1]))
            end += 2
        yield tuple(parts), end, quoted
        index = end


def references(text: str) -> SqlReferences:
    """Combine lexical references with separate catalog/counter enforcement.

    This is not an arbitrary server-language parser. Dynamic execution,
    prepared statements and DDL explicitly fail closed, never disappear.
    """
    raw = list(_tokenize(text))
    items = [
        (kind, value)
        for kind, value in raw
        if kind not in tokens.Whitespace and kind not in tokens.Comment
    ]
    result = SqlReferences(operators=_operators(raw))
    for index, (kind, value) in enumerate(items):
        if kind in tokens.Keyword:
            keyword = value.lower().split()[0]
            if keyword == "show":
                _show_setting(items, index + 1, result)
            if keyword in DYNAMIC | DDL:
                result.opaque.add(keyword)
            if keyword in {"insert", "update", "delete", "truncate"}:
                result.writes = True
    for name, end, quoted in _qualified(items):
        result.names.add(name)
        if end >= len(items) or items[end][1] != "(":
            continue
        if quoted or len(name) > 1 or name[-1] not in SYNTAX_CALLS:
            result.calls.add(name)
        # set_config returns the effective setting value, not just a write count.
        if name[-1] in {"current_setting", "set_config"}:
            _setting(items, end + 1, result)
    return result


def _show_setting(items: Sequence[Token], index: int, result: SqlReferences) -> None:
    if index < len(items) and _identifier(*items[index]):
        name, end, _quoted = next(_qualified(items[index:]))
        setting = ".".join(name).lower()
        end += index
        if setting != "all" and (end == len(items) or items[end][1] == ";"):
            result.settings.add(setting)
            return
    # SHOW ALL and unresolved syntax cannot establish absence of actor access.
    result.opaque.add("unresolved SHOW setting")


def _operators(items: Sequence[Token]) -> set[str]:
    # sqlparse tokenizes '?' as a placeholder even inside a PostgreSQL operator.
    # Reconstruct contiguous operator characters before discarding whitespace.
    allowed = frozenset("+-*/<>=~!@#%^&|`?")
    result = set()
    pending = ""
    for kind, value in [*items, (None, " ")]:
        if kind not in tokens.Comment and value and set(value) <= allowed:
            pending += value
        else:
            if pending:
                result.add(pending)
                pending = ""
            if kind in tokens.Literal.Number and value.startswith("-"):
                result.add("-")
    return result


def _setting(items: Sequence[Token], index: int, result: SqlReferences) -> None:
    if index >= len(items) or items[index][0] not in tokens.Literal.String.Single:
        result.opaque.add("computed setting name")
        return
    result.settings.add(items[index][1][1:-1].replace("''", "'").lower())
    end = index + 1
    # pg_get_functiondef adds this built-in text coercion to SQL-standard bodies.
    if [value for _kind, value in items[end : end + 2]] == ["::", "text"]:
        end += 2
    if end >= len(items) or items[end][1] not in {",", ")"}:
        result.opaque.add("computed setting name")
