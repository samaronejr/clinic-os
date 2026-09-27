"""Typed boundary for sqlparse's string-only lexer interface."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, cast

from sqlparse.lexer import tokenize

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlparse import tokens

_tokenize = cast("Callable[[str], Iterator[tuple[tokens._TokenType, str]]]", tokenize)


@lru_cache(maxsize=4096)
def _lexemes(source: str) -> tuple[tuple[tokens._TokenType, str], ...]:
    return tuple(_tokenize(source))


def sql_tokens(source: str) -> Iterator[tuple[tokens._TokenType, str]]:
    """Share exact immutable lexical results, never a state-dependent census."""
    return iter(_lexemes(source))
