"""Prove a machine_principal member's result is gated by principal_scope.

Token-level and fail closed: any shape the analyzer cannot prove is a
violation. The accepted shapes are deliberately narrow, and nothing runs
before the gate.

- SQL body: exactly one ``SELECT <c1> AND ... AND <cn>`` with no FROM, WHERE,
  set operation or top-level OR, where a conjunct is exactly
  ``clinic_app.principal_scope(...) IS NOT NULL``.
- plpgsql body: a DECLARE section without initialisers or cursors, then an
  outer block whose first statement is ``v := clinic_app.principal_scope(...);``
  and whose second is exactly ``IF v IS NULL THEN RETURN false|NULL; END IF;``.
  Outer EXCEPTION handlers may only refuse.
- No setting writes anywhere in the body: SET, SET LOCAL, RESET, set_config
  and SET ... FROM CURRENT are refused.
- The member returns a single boolean with no OUT parameters, so a refusal is
  never read as a grant and every value leaves through RETURN.
- Every uuid parameter is bound to a gate: each gate's clinic (second)
  argument is a bare reference to a named parameter, and the set of gated
  parameters is exactly the member's uuid parameters. Parameters are uuid or
  text only: any other type (an array, a domain, a composite) could carry a
  clinic past the binding, so it fails closed. The first argument is
  free: principal_scope requires ``db_identity = session_user`` and each login
  binds at most one principal, so it can only name the login's own principal.

Behind the gate everything else can only narrow a grant principal_scope
already validated for every clinic the member was asked about.

PostgreSQL does not order a SQL member's AND conjuncts, so a conjunct may
still run before the gate. What that conjunct can do is bounded by the
census rather than this prover: every function in a member's closure must be
STABLE or IMMUTABLE, so PostgreSQL refuses writes and utility commands and no
volatile function is reachable (test_service_principals, review round 8). The
only exceptions are the pinned VOLATILE gates, principal_scope and
principal_has, which need a fresh snapshot per call for immediate revocation
(VOLATILE_GATES, review round 9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlparse import lexer, tokens

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

Token = tuple[object, str]
# A gate call's tokens, and every token that runs before it in the body.
Gate = tuple[list[Token], list[Token]]
_tokenize = cast("Callable[[str], Iterable[Token]]", lexer.tokenize)

NOT_GATED = "result is not gated by principal_scope"
EARLY_RETURN = "returns before the principal_scope gate"
UNBOUND_CLINIC = "principal_scope gate does not check a clinic parameter"
UNGATED_UUID = "a uuid parameter is not gated by principal_scope"
SETTING_WRITE = "writes a setting"
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
# DECLARE entries may only name variables: an initialiser, constant or cursor
# would run (or bind) code before the gate.
DECLARE_FORBIDDEN = frozenset({":=", "=", "default", "constant", "cursor", "("})
SETTING_WRITERS = frozenset({"set", "reset", "set_config"})
# Parameter types the binding can account for: uuid parameters are bound to
# gates, and the executed check passes foreign clinic ids through text ones.
PARAMETER_TYPES = frozenset({"uuid", "text"})
CLOSERS = {"end": {"begin", "case"}, "end if": {"if"}, "end loop": {"loop"}}
CLOSERS["end case"] = {"case"}
UNBALANCED_EXPRESSION = "unbalanced expression"
UNBALANCED_BLOCK = "unbalanced block"


class _UnprovableError(Exception):
    pass


class _RefusedError(Exception):
    """A complete violation (kind and detail) ending the proof."""


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


def setting_writes(source: str) -> list[str]:
    """SET/RESET words and set_config calls anywhere in a body."""
    words = {
        # A quoted identifier ("set_config") still names the function.
        value[1:-1].lower() if kind is tokens.Literal.String.Symbol else _word(item)
        for item in _items(source)
        for kind, value in [item]
    }
    return sorted(word for word in words if word.split(" ", 1)[0] in SETTING_WRITERS)


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


def _test_call(items: Sequence[Token], suffix: list[str]) -> list[Token] | None:
    """The gate call when ``items`` is exactly ``<gate call> <suffix>``."""
    end = _gate_call(items)
    if end is not None and [_word(item) for item in items[end:]] == suffix:
        return list(items[:end])
    return None


def _sql_gates(body: str) -> tuple[list[str], list[Gate]]:
    items = _items(body)
    if items and items[-1][1] == ";":
        items.pop()
    if not items or _word(items[0]) != "select":
        return [NOT_GATED + ": body is not a single SELECT"], []
    try:
        calls = [
            call
            for conjunct in _split(items[1:], "and")
            if (call := _test_call(conjunct, ["is", "not null"]))
        ]
    except _UnprovableError as error:
        return [f"{NOT_GATED}: {error}"], []
    if not calls:
        return [NOT_GATED + ": no principal_scope IS NOT NULL conjunct"], []
    # A SQL body has no assignments: nothing runs before the gate.
    return [], [(call, []) for call in calls]


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


def _assigned_gate(
    statement: Sequence[Token], before: list[Token]
) -> tuple[str, Gate] | None:
    if len(statement) < 3 or _word(statement[1]) not in {":=", "="}:
        return None
    end = _gate_call(statement[2:])
    if end is None or end + 2 != len(statement):
        return None
    kind, value = statement[0]
    if kind not in tokens.Name or not _word(statement[0]):
        return None
    return value.lower(), (list(statement[2:]), [*before, statement[0]])


def _plpgsql_gate(body: str) -> Gate:
    items = _items(body)
    words = [_word(item) for item in items]
    if items and words[-1] == ";":
        items.pop()
        words.pop()
    if "begin" not in words or words[-1] != "end":
        raise _RefusedError(NOT_GATED + ": no outer block")
    begin = words.index("begin")
    if forbidden := DECLARE_FORBIDDEN.intersection(words[:begin]):
        detail = " ".join(sorted(forbidden))
        raise _RefusedError(NOT_GATED + ": DECLARE runs code: " + detail)
    try:
        statements, handlers = _statements(items[begin + 1 : -1])
    except _UnprovableError as error:
        problem = f"{NOT_GATED}: {error}"
        raise _RefusedError(problem) from error
    if not _returns_only_refusals(handlers):
        raise _RefusedError(EARLY_RETURN + ": an outer handler returns a value")
    assignment = (
        _assigned_gate(statements[0], list(items[:begin])) if statements else None
    )
    if assignment is None:
        raise _RefusedError(NOT_GATED + ": the first statement is not the gate")
    variable, gate = assignment
    refusal = [_word(item) for item in statements[1]] if len(statements) > 1 else []
    expected = ["if", variable, "is", "null", "then", "return"]
    if refusal[:6] != expected or refusal[6:] not in (
        [word, ";", "end if"] for word in REFUSALS
    ):
        raise _RefusedError(NOT_GATED + ": the gate is not followed by its refusal")
    return gate


def _bound_clinic(gate: Gate, parameters: Sequence[str]) -> str | None:
    """The member parameter the gate checks, if it is bare and untouched before."""
    call, before = gate
    try:
        arguments = _split(call[4:-1], ",")
    except _UnprovableError:
        return None
    if len(arguments) != 2 or len(arguments[1]) != 1:
        return None
    kind, value = arguments[1][0]
    name = value.lower()
    if kind not in tokens.Name or name not in parameters:
        return None
    # Any earlier mention (DECLARE shadowing or ALIAS FOR $n, or the gate
    # variable itself) could rebind it; only an untouched parameter is proven.
    position = f"${parameters.index(name) + 1}"
    if {name, position} & {_word(item) for item in before}:
        return None
    return name


def _gates(language: str, source: str) -> list[Gate]:
    if language == "sql":
        problems, gates = _sql_gates(source)
        if problems:
            raise _RefusedError(problems[0])
        return gates
    if language == "plpgsql":
        return [_plpgsql_gate(source)]
    raise _RefusedError(NOT_GATED + ": language " + language)


def _prove(
    language: str,
    source: str,
    *,
    returns_boolean: bool,
    parameters: Sequence[str],
    types: Sequence[str],
) -> None:
    if not returns_boolean:
        raise _RefusedError(NOT_GATED + ": does not return a single boolean")
    if len(parameters) != len(types):
        raise _RefusedError(UNGATED_UUID + ": parameters are not all named")
    if unprovable := sorted(set(types) - PARAMETER_TYPES):
        detail = ", ".join(unprovable)
        raise _RefusedError(UNGATED_UUID + ": unprovable parameter type " + detail)
    gates = _gates(language, source)
    bound = {clinic for gate in gates if (clinic := _bound_clinic(gate, parameters))}
    uuids = {
        name for name, kind in zip(parameters, types, strict=True) if kind == "uuid"
    }
    if not bound or not bound <= uuids:
        raise _RefusedError(UNBOUND_CLINIC + ": not a bare uuid parameter")
    if uuids - bound:
        raise _RefusedError(UNGATED_UUID + ": " + ", ".join(sorted(uuids - bound)))


def member_gate(
    *,
    language: str,
    source: str,
    returns_boolean: bool,
    parameters: Sequence[str],
    types: Sequence[str],
) -> list[str]:
    """Violations proving (or failing to prove) the principal_scope gate."""
    writes = setting_writes(source)
    problems = [SETTING_WRITE + ": " + ", ".join(writes)] if writes else []
    try:
        _prove(
            language,
            source,
            returns_boolean=returns_boolean,
            parameters=parameters,
            types=types,
        )
    except _RefusedError as error:
        problems.append(str(error))
    return problems
