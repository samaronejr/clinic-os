"""Fail closed on literal coercions through catalog-derived clock input types."""

from __future__ import annotations

import re

from django.db import connection
from sqlparse import tokens

from .clock_tokens import sql_tokens

# String syntax, not a vocabulary of values such as now/today. Every literal
# coercion through an affected input function remains potentially clock-reading.
LITERAL = r"(?:E)?'(?:[^']|'')*'|\$(?:\w*)\$[\s\S]*?\$(?:\w*)\$"


def temporal_input_types(readers: frozenset[int]) -> frozenset[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "WITH RECURSIVE affected(oid) AS ("
            "SELECT oid FROM pg_type WHERE typinput=ANY(%s) UNION "
            "SELECT t.oid FROM pg_type t JOIN affected a ON t.typbasetype=a.oid) "
            "SELECT n.nspname,t.typname,format_type(t.oid,NULL) FROM affected a "
            "JOIN pg_type t ON t.oid=a.oid "
            "JOIN pg_namespace n ON n.oid=t.typnamespace",
            [list(readers)],
        )
        spellings = set()
        for schema, name, formatted in cursor.fetchall():
            spellings.update(
                {
                    str(name),
                    str(formatted),
                    f"{schema}.{name}",
                    f'"{schema}"."{name}"',
                    f'{schema}."{name}"',
                    f'"{name}"',
                }
            )
    return frozenset(spellings)


class LiteralInputs:
    def __init__(self, readers: frozenset[int]) -> None:
        spellings = temporal_input_types(readers)
        precision = r"(?:\s*\(\s*\d+\s*\))?"
        names = []
        for spelling in sorted(spellings):
            head, *tail = spelling.split(" ")
            names.append(
                re.escape(head).replace(r"\.", r"\s*\.\s*")
                + precision
                + "".join(r"\s+" + re.escape(word) for word in tail)
            )
        kind = r"(?<![\w$])(?:" + "|".join(names) + r")(?![\w$])"
        literal = "(?:" + LITERAL + ")"
        self.patterns = (
            tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in (
                    literal + r"\s*\)*\s*::\s*" + kind,
                    r"\bCAST\s*\(\s*\(*\s*" + literal + r"\s*\)*\s+AS\s+" + kind,
                    kind + r"\s+" + literal,
                    kind + r"(?:\s+NOT\s+NULL)?\s*(?::=|=|\bDEFAULT\b)\s*" + literal,
                )
            )
            if spellings
            else ()
        )

    def unresolved(self, source: str) -> bool:
        # Removing comments handles legal whitespace without interpreting values.
        normalized = "".join(
            value if kind not in tokens.Comment else " "
            for kind, value in sql_tokens(source)
        )
        return any(pattern.search(normalized) for pattern in self.patterns)
