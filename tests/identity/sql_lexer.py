"""One left-to-right SQL lexer for the actor rule and its boundary guard.

PostgreSQL's scanner (``src/backend/parser/scan.l``) decides at every position
whether it is in code, a quoted form or a comment, reading left to right. A
text classifier that removes comments first and literals second (or the
other way round) disagrees with it as soon as a literal holds ``--`` or
``/*``, or an escape string holds ``\\'``. This lexer walks the text once
with the scanner's rules under ``standard_conforming_strings = on``:

- ``'...'`` with ``''`` doubling, also after ``N``;
- ``E'...'`` with backslash escapes (``\\'`` and ``\\\\`` included);
- ``U&'...'`` and ``U&"..."`` (doubling, backslash not special);
- ``B'...'`` and ``X'...'`` (no escapes, no doubling);
- ``"..."`` identifiers with ``""`` doubling;
- dollar-quoted strings ``$$...$$`` and ``$tag$...$tag$`` (a tag only at a
  token start, so ``a$b$`` stays an identifier and ``$1`` a parameter),
  whose body is lexed again as SQL, because in this code base they hold
  function bodies and DO blocks;
- ``--`` line comments and nested ``/* /* */ */`` block comments.

The views it returns keep every position (same length as the text), so a
match in a view is a match at the same offset of the text:

- ``code``: comments blanked; quoted forms verbatim; dollar bodies replaced
  by their own ``code``;
- ``bare``: ``code`` with every string literal blanked too (a prefix such as
  ``E`` or ``U&`` stays visible); identifiers verbatim.

Anything it cannot settle is reported in ``unsettled`` and never guessed:
an unterminated quote, identifier, dollar-quoted string or block comment,
and any mention of ``standard_conforming_strings`` (with it off, a backslash
escapes a quote in a plain literal and every boundary above can move). An
unsettled text hides nothing: both views are the text itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

# The scanner's identifier characters (bytes >= 0x80 are identifier letters).
_IDENT: Final = re.compile(r"[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_$\x80-\U0010ffff]*")
_NUMBER: Final = re.compile(r"[0-9][A-Za-z0-9_.]*")
_PARAM: Final = re.compile(r"\$[0-9]+")
_DOLLAR: Final = re.compile(
    r"\$(?:[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_\x80-\U0010ffff]*)?\$"
)
# Runs of characters that start nothing special.
_PLAIN: Final = re.compile(r"""[^'"$\-/A-Za-z0-9_\x80-\U0010ffff]+|-(?!-)|/(?!\*)""")
# Quoted forms, each from its opening quote; possessive, like the scanner's
# longest match: ``''`` inside a literal is always a doubled quote.
_STANDARD: Final = re.compile(r"'(?:[^']|'')*+'")
_ESCAPED: Final = re.compile(r"'(?:[^'\\]|''|\\.)*+'", re.DOTALL)
_BITS: Final = re.compile(r"'[^']*+'")
_IDENTIFIER: Final = re.compile(r'"(?:[^"]|"")*+"')
_LINE: Final = re.compile(r"--[^\n\r]*")
# scan.l ``quotecontinue``: whitespace holding a line break (``--`` comments
# count as whitespace) between two quoted parts continues the same string,
# in the same state (an escape string stays one).
_CONTINUE: Final = re.compile(
    r"(?:[ \t\f\v]|--[^\n\r]*)*+[\n\r](?:[ \t\n\r\f\v]|--[^\n\r]*[\n\r])*+(?=')"
)
_BLOCK_EDGE: Final = re.compile(r"/\*|\*/")
_NON_STANDARD: Final = re.compile(r"standard_conforming_strings", re.IGNORECASE)

# Span kinds.
CODE: Final = "code"
STRING: Final = "string"
IDENTIFIER: Final = "identifier"
COMMENT: Final = "comment"
DOLLAR: Final = "dollar"


@dataclass(frozen=True, slots=True)
class Span:
    """One lexed piece: ``text[start:end]`` is of ``kind``; for a string,
    ``body`` is where its quoted part starts (after any prefix)."""

    kind: str
    start: int
    end: int
    body: int = 0


@dataclass(frozen=True, slots=True)
class Lexed:
    """The text's views (same length as the text) and what could not be
    settled. ``spans`` are the top-level pieces, in order."""

    code: str
    bare: str
    unsettled: tuple[str, ...]
    spans: tuple[Span, ...]


class _Unsettled(Exception):  # noqa: N818 - control flow inside the lexer
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _blank(text: str) -> str:
    """Spaces for every character except line breaks (kept for line anchors)."""
    return re.sub(r"[^\n\r]", " ", text)


def _end(pattern: re.Pattern[str], text: str, position: int, what: str) -> int:
    match = pattern.match(text, position)
    if match is None:
        message = f"an unterminated {what}"
        raise _Unsettled(message)
    return match.end()


def _literal(text: str, position: int, pattern: re.Pattern[str]) -> int:
    """End of a string literal whose quoted part opens at ``position``,
    continuation parts included."""
    end = _end(pattern, text, position, "string literal")
    while more := _CONTINUE.match(text, end):
        end = _end(pattern, text, more.end(), "string literal")
    return end


def _string(text: str, position: int) -> Span:
    return Span(STRING, position, _literal(text, position, _STANDARD), position)


def _identifier(text: str, position: int) -> Span:
    return Span(IDENTIFIER, position, _end(_IDENTIFIER, text, position, "identifier"))


def _line(text: str, position: int) -> Span:
    """``--`` (a lone ``-`` is plain) to the end of the line."""
    return Span(COMMENT, position, _end(_LINE, text, position, "comment"))


def _block(text: str, position: int) -> Span:
    """``/*`` (a lone ``/`` is plain) to its matching ``*/``, nesting."""
    depth = 0
    for edge in _BLOCK_EDGE.finditer(text, position):
        depth += 1 if edge.group() == "/*" else -1
        if depth == 0:
            return Span(COMMENT, position, edge.end())
    message = "an unterminated block comment"
    raise _Unsettled(message)


def _dollar(text: str, position: int) -> Span:
    """A parameter ``$1``, a dollar-quoted string, or a lone ``$``."""
    if param := _PARAM.match(text, position):
        return Span(CODE, position, param.end())
    delimiter = _DOLLAR.match(text, position)
    if delimiter is None:
        return Span(CODE, position, position + 1)
    close = text.find(delimiter.group(), delimiter.end())
    if close < 0:
        message = "an unterminated dollar-quoted string"
        raise _Unsettled(message)
    return Span(DOLLAR, position, close + len(delimiter.group()), delimiter.end())


def _word(text: str, position: int) -> Span:
    """A number, an identifier, or a string with a prefix (``E``, ``B``,
    ``X``, ``N``, ``U&``) that only counts at a token start."""
    if text[position].isascii() and text[position].isdigit():
        return Span(CODE, position, _end(_NUMBER, text, position, "number"))
    head = text[position : position + 3]
    first = head[:1].lower()
    if head[1:2] == "'" and first in _PREFIXED:
        return Span(
            STRING,
            position,
            _literal(text, position + 1, _PREFIXED[first]),
            position + 1,
        )
    if first == "u" and head[1:2] == "&" and head[2:3] == "'":
        return Span(
            STRING, position, _literal(text, position + 2, _STANDARD), position + 2
        )
    if first == "u" and head[1:2] == "&" and head[2:3] == '"':
        return Span(
            IDENTIFIER, position, _end(_IDENTIFIER, text, position + 2, "identifier")
        )
    return Span(CODE, position, _end(_IDENT, text, position, "identifier"))


_PREFIXED: Final = {"e": _ESCAPED, "b": _BITS, "x": _BITS, "n": _STANDARD}
_DISPATCH: Final = {
    "'": _string,
    '"': _identifier,
    "-": _line,
    "/": _block,
    "$": _dollar,
}


def _scan(text: str) -> list[Span]:
    spans: list[Span] = []
    position = 0
    while position < len(text):
        plain = _PLAIN.match(text, position)
        span = (
            Span(CODE, position, plain.end())
            if plain
            else _DISPATCH.get(text[position], _word)(text, position)
        )
        spans.append(span)
        position = span.end
    return spans


@lru_cache(maxsize=8192)
def lex(text: str) -> Lexed:
    """Lex one SQL-bearing text (see the module docstring)."""
    unsettled: list[str] = []
    if _NON_STANDARD.search(text):
        unsettled.append(
            "standard_conforming_strings is named, so string boundaries depend "
            "on a setting the lexer does not follow"
        )
    try:
        spans = _scan(text)
    except _Unsettled as error:
        return Lexed(text, text, (*unsettled, error.reason), ())
    code: list[str] = []
    bare: list[str] = []
    for span in spans:
        piece = text[span.start : span.end]
        if span.kind == COMMENT:
            code.append(_blank(piece))
            bare.append(_blank(piece))
        elif span.kind == STRING:
            code.append(piece)
            prefix, quoted = text[span.start : span.body], text[span.body : span.end]
            bare.append(prefix + _blank(quoted))
        elif span.kind == DOLLAR:
            close = span.end - (span.body - span.start)
            inner = lex(text[span.body : close])
            unsettled.extend(
                f"{reason} in a dollar-quoted body" for reason in inner.unsettled
            )
            head, tail = text[span.start : span.body], text[close : span.end]
            code.append(head + inner.code + tail)
            bare.append(_blank(head) + inner.bare + _blank(tail))
        else:
            code.append(piece)
            bare.append(piece)
    if unsettled:
        return Lexed(text, text, tuple(unsettled), tuple(spans))
    return Lexed("".join(code), "".join(bare), (), tuple(spans))
