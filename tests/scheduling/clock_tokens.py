"""Typed boundary for sqlparse's string-only lexer interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlparse.lexer import tokenize

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlparse import tokens

sql_tokens = cast("Callable[[str], Iterator[tuple[tokens._TokenType, str]]]", tokenize)
