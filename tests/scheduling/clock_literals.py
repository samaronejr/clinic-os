"""Decode SQL string values independently of the context that consumes them."""

from __future__ import annotations

from functools import lru_cache

from django.db import connection
from sqlparse import tokens

from .clock_tokens import sql_tokens


def _quoted(kind: tokens._TokenType, value: str) -> bool:
    return kind in tokens.Literal.String.Single or (
        kind in tokens.Literal and value.startswith("$")
    )


@lru_cache(maxsize=2048)
def _decode(raw: str, *, standard_strings: bool) -> str:
    if raw.startswith("$"):
        delimiter = raw[: raw.index("$", 1) + 1]
        assert raw.endswith(delimiter)
        return raw[len(delimiter) : -len(delimiter)]
    if raw.startswith("'") and (standard_strings or "\\" not in raw):
        return raw[1:-1].replace("''", "'")
    # E/U&/UESCAPE and nonstandard escaped values use the server's decoder.
    # The raw expression contains only lexer-identified literal tokens.
    with connection.cursor() as cursor:
        cursor.execute("SELECT " + raw)
        row = cursor.fetchone()
    assert row is not None
    assert isinstance(row[0], str)
    return row[0]


def _raw_literal(
    stream: list[tuple[tokens._TokenType, str]], index: int
) -> tuple[str, int, int]:
    start = index
    prefix = ""
    if index and stream[index - 1][1].casefold() == "e":
        prefix, start = "E", index - 1
    elif (
        index > 1
        and stream[index - 2][1].casefold() == "u"
        and stream[index - 1][1] == "&"
    ):
        prefix, start = "U&", index - 2
    raw = prefix + stream[index][1]
    following = index + 1
    while following < len(stream) and stream[following][0] in tokens.Whitespace:
        following += 1
    if (
        prefix == "U&"
        and following < len(stream)
        and stream[following][1].casefold() == "uescape"
    ):
        end = following + 1
        while end < len(stream) and stream[end][0] in tokens.Whitespace:
            end += 1
        assert end < len(stream)
        assert stream[end][0] in tokens.Literal.String.Single
        raw += " UESCAPE " + stream[end][1]
        index = end
    return raw, start, index


# Immutable decoded text is identical across template clones. The setting is
# part of the key; no type, relation or clock-classification verdict is cached.
@lru_cache(maxsize=2048)
def literal_values(source: str, *, standard_strings: bool) -> tuple[str, ...]:
    stream = list(sql_tokens(source))
    result: list[str] = []
    index = 0
    previous_end = -1
    while index < len(stream):
        kind, value = stream[index]
        if _quoted(kind, value):
            raw, start, end = _raw_literal(stream, index)
            decoded = _decode(raw, standard_strings=standard_strings)
            between = stream[previous_end + 1 : start]
            # PostgreSQL's newline-separated adjacent strings form one value.
            adjacent = (
                previous_end >= 0
                and all(
                    kind in tokens.Whitespace or kind in tokens.Comment
                    for kind, _ in between
                )
                and any("\n" in text for _, text in between)
            )
            if adjacent:
                result[-1] += decoded
            else:
                result.append(decoded)
            previous_end = index = end
        index += 1
    return tuple(result)
