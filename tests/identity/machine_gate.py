"""Prove a machine_principal member's result is gated by principal_scope.

Token-level and fail closed: any shape the analyzer cannot prove is a
violation. The accepted shapes are deliberately narrow.

- SQL body: exactly one ``SELECT <c1> AND ... AND <cn>`` with no FROM, WHERE,
  set operation or top-level OR, where one conjunct is exactly
  ``clinic_app.principal_scope(...) IS NOT NULL``.
- plpgsql body: in the outer block, before any RETURN that is not
  ``RETURN false``/``RETURN NULL``, a top-level statement pair
  ``v := clinic_app.principal_scope(...);`` immediately followed by
  ``IF ... OR v IS NULL OR ... THEN RETURN false; END IF;``. A top-level
  ``RETURN <gated conjunction>;`` also counts, in the SQL sense.
  Outer EXCEPTION handlers may only refuse.
- The member must return a single boolean with no OUT parameters, so a
  refusal can never be read as a grant and every value leaves through RETURN.

Inside the gate everything else can only narrow a grant principal_scope
already validated, whatever settings, arguments or data it reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlparse import lexer, tokens

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

Token = tuple[object, str]
_tokenize = cast("Callable[[str], Iterable[Token]]", lexer.tokenize)

NOT_GATED = "result is not gated by principal_scope"
EARLY_RETURN = "returns before the principal_scope gate"
REFUSALS = frozenset({"false", "null"})
# Top-level words that end or widen an expression: the gate cannot be proven
# a conjunct of the returned value across them.
BARRIERS = frozenset(
    {
        "or",
        "between",
        "from",
        "where",
        "group",
        "having",
        "order",
        "limit",
        "offset",
        "fetch",
        "window",
        "union",
        "intersect",
        "except",
        "into",
        "select",
        ",",
        ";",
    }
)
CLOSERS = {"end": {"begin", "case"}, "end if": {"if"}, "end loop": {"loop"}}
CLOSERS["end case"] = {"case"}
UNBALANCED_EXPRESSION = "unbalanced expression"
UNBALANCED_BLOCK = "unbalanced block"


class _UnprovableError(Exception):
    pass


def _word(token: Token) -> str:
    kind, value = token
    if kind in tokens.Literal.String:
        return ""
    return " ".join(value.lower().split())


def _items(text: str) -> list[Token]:
    return [
        (kind, value)
        for kind, value in _tokenize(text)
        if kind not in tokens.Whitespace and kind not in tokens.Comment
    ]


def _split(items: Sequence[Token], separator: str) -> list[list[Token]]:
    """Split at top-level ``separator``; barriers other than it are unprovable."""
    parts: list[list[Token]] = [[]]
    depth = 0
    for item in items:
        word = _word(item)
        if word in {"(", "[", "case"}:
            depth += 1
        elif word in {")", "]", "end"}:
            depth -= 1
            if depth < 0:
                raise _UnprovableError(UNBALANCED_EXPRESSION)
        elif depth == 0 and word == separator:
            parts.append([])
            continue
        elif depth == 0 and word in BARRIERS:
            raise _UnprovableError("top-level " + word)
        parts[-1].append(item)
    if depth:
        raise _UnprovableError(UNBALANCED_EXPRESSION)
    return parts


def _gate_call(items: Sequence[Token]) -> int | None:
    """Index after ``clinic_app.principal_scope(...)`` at the start, or None."""
    words = [_word(item) for item in items[:4]]
    if words != ["clinic_app", ".", "principal_scope", "("]:
        return None
    depth = 0
    for index in range(3, len(items)):
        word = _word(items[index])
        depth += word == "("
        depth -= word == ")"
        if depth == 0:
            return index + 1
    return None


def _is_test(items: Sequence[Token], suffix: list[str]) -> bool:
    end = _gate_call(items)
    return end is not None and [_word(item) for item in items[end:]] == suffix


def _gated_conjunction(items: Sequence[Token]) -> bool:
    return any(
        _is_test(conjunct, ["is", "not null"]) for conjunct in _split(items, "and")
    )


def sql_gate(body: str) -> list[str]:
    items = _items(body)
    if items and items[-1][1] == ";":
        items.pop()
    if not items or _word(items[0]) != "select":
        return [NOT_GATED + ": body is not a single SELECT"]
    try:
        gated = _gated_conjunction(items[1:])
    except _UnprovableError as error:
        return [f"{NOT_GATED}: {error}"]
    return [] if gated else [NOT_GATED + ": no principal_scope IS NOT NULL conjunct"]


def _nest(word: str, stack: list[str]) -> None:
    """Track plpgsql block nesting; a mismatched closer is unprovable."""
    if word in {"begin", "if", "loop", "case"}:
        stack.append(word)
    elif word.startswith("if "):
        # sqlparse lexes IF EXISTS / IF NOT EXISTS as one keyword.
        stack.append("if")
    elif word in CLOSERS and (not stack or stack.pop() not in CLOSERS[word]):
        raise _UnprovableError(UNBALANCED_BLOCK)


def _statements(items: Sequence[Token]) -> tuple[list[list[Token]], list[Token]]:
    """Top-level statements of a block body, and its EXCEPTION section."""
    statements: list[list[Token]] = [[]]
    stack: list[str] = []
    depth = 0
    for index, item in enumerate(items):
        word = _word(item)
        if word == "exception" and not stack and not depth and not statements[-1]:
            # At statement start this opens the handlers; RAISE EXCEPTION does not.
            return statements[:-1], list(items[index + 1 :])
        depth += (word == "(") - (word == ")")
        if depth < 0:
            raise _UnprovableError(UNBALANCED_EXPRESSION)
        _nest(word, stack)
        statements[-1].append(item)
        if word == ";" and not stack and not depth:
            statements[-1].pop()
            statements.append([])
    if stack or depth or statements[-1]:
        raise _UnprovableError(UNBALANCED_BLOCK)
    return statements[:-1], []


def _returns_only_refusals(items: Sequence[Token]) -> bool:
    words = [_word(item) for item in items]
    refusals = {(word, ";") for word in REFUSALS}
    return all(
        tuple(words[index + 1 : index + 3]) in refusals
        for index, word in enumerate(words)
        if word == "return"
    )


def _assigned_gate(statement: Sequence[Token]) -> str | None:
    if len(statement) < 3 or _word(statement[1]) not in {":=", "="}:
        return None
    end = _gate_call(statement[2:])
    if end is None or end + 2 != len(statement):
        return None
    kind, value = statement[0]
    return value.lower() if kind in tokens.Name and _word(statement[0]) else None


def _gate_if(statement: Sequence[Token], variable: str | None) -> bool:
    words = [_word(item) for item in statement]
    if (
        len(words) < 7
        or words[0] != "if"
        or words[-5:-3] != ["then", "return"]
        or words[-3] not in REFUSALS
        or words[-2:] != [";", "end if"]
    ):
        return False
    condition = statement[1:-5]
    if "then" in (_word(item) for item in condition):
        return False
    for disjunct in _split(condition, "or"):
        if _is_test(disjunct, ["is", "null"]):
            return True
        if variable and [_word(item) for item in disjunct] == [variable, "is", "null"]:
            return True
    return False


def plpgsql_gate(body: str) -> list[str]:
    items = _items(body)
    words = [_word(item) for item in items]
    if items and words[-1] == ";":
        items.pop()
        words.pop()
    if "begin" not in words or words[-1] != "end":
        return [NOT_GATED + ": no outer block"]
    try:
        statements, handlers = _statements(items[words.index("begin") + 1 : -1])
        if not _returns_only_refusals(handlers):
            return [EARLY_RETURN + ": an outer handler returns a value"]
        variable = None
        for statement in statements:
            gated = _gate_if(statement, variable) or (
                _word(statement[0]) == "return" and _gated_conjunction(statement[1:])
            )
            if gated:
                return []
            if not _returns_only_refusals(statement):
                return [EARLY_RETURN]
            variable = _assigned_gate(statement)
    except _UnprovableError as error:
        return [f"{NOT_GATED}: {error}"]
    return [NOT_GATED + ": no principal_scope gate in the outer block"]


def member_gate(*, language: str, source: str, returns_boolean: bool) -> list[str]:
    """Violations proving (or failing to prove) the principal_scope gate."""
    if not returns_boolean:
        return [NOT_GATED + ": does not return a single boolean through RETURN"]
    if language == "sql":
        return sql_gate(source)
    if language == "plpgsql":
        return plpgsql_gate(source)
    return [NOT_GATED + ": language " + language]
