"""Expected text of identity 0051's staff-context fence on one policy expression.

0051 narrows every clinic_app policy that applies to clinic_app with
``enterprise_staff_context()``. PostgreSQL flattens the fence into a top-level
AND chain, so the fenced text is that chain plus one term; any other expression
becomes a two-term chain. The name is schema-qualified only when clinic_app is
off the search_path of the reading session.
"""

from __future__ import annotations


def fenced(expression: str, *, qualified: bool = False) -> str:
    """Return ``pg_get_expr`` of ``(expression) AND enterprise_staff_context()``."""
    fence = ("clinic_app." if qualified else "") + "enterprise_staff_context()"
    if _and_chain(expression):
        return f"{expression[:-1]} AND {fence})"
    return f"({expression} AND {fence})"


def _and_chain(expression: str) -> bool:
    """Whether the whole expression is one parenthesized top-level AND chain."""
    if not (expression.startswith("(") and expression.endswith(")")):
        return False
    depth = 0
    quoted = False
    found = False
    for index, char in enumerate(expression):
        if char == "'":
            quoted = not quoted
        elif quoted:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(expression) - 1:
                return False
        elif depth == 1 and expression.startswith(" AND ", index):
            found = True
    return found
