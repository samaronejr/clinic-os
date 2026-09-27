"""Enforced source boundary for SQL text the clock census cannot resolve safely."""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from sqlparse import tokens

from .clock_source import DYNAMIC, source_fragments
from .clock_tokens import sql_tokens

if TYPE_CHECKING:
    from pathlib import Path


def _setting_is_unresolved(
    stream: list[tuple[tokens._TokenType, str]], index: int
) -> bool:
    if index + 3 >= len(stream):
        return True
    kind, argument = stream[index + 2]
    return (
        kind not in tokens.Literal.String.Single
        or stream[index + 3][1] not in {",", ")"}
        or any(marker in argument for marker in (DYNAMIC, "{", "%s"))
    )


def _word_violations(
    stream: list[tuple[tokens._TokenType, str]], index: int
) -> set[str]:
    word = stream[index][1].lower().strip('"')
    previous = stream[index - 1][1].upper() if index else ""
    following = stream[index + 1][1].upper() if index + 1 < len(stream) else ""
    if (
        word == "execute"
        and following
        and previous not in {"GRANT", "REVOKE"}
        and following
        not in {
            "FUNCTION",
            "PROCEDURE",
            "ON",
        }
    ):
        return {"dynamic-or-prepared-execute"}
    if (
        word in {"current_setting", "set_config"}
        and following == "("
        and _setting_is_unresolved(stream, index)
    ):
        return {"unresolvable-setting-name"}
    if (
        word == "set_config"
        and following == "("
        and index + 2 < len(stream)
        and stream[index + 2][1].strip("'").lower() == "search_path"
    ):
        return {"mutable-search-path"}
    if re.fullmatch(r"pg_temp(?:_\d+)?", word) and following == ".":
        return {"temporary-reference"}
    if (
        word == "language"
        and following
        and following.strip("'\"").lower() not in {"sql", "plpgsql"}
    ):
        return {"unsupported-language"}
    return set()


def _nested_body(stream: list[tuple[tokens._TokenType, str]], index: int) -> str:
    kind, value = stream[index]
    if kind not in tokens.Literal:
        return ""
    delimiter = re.match(r"\$[\w]*\$", value)
    if delimiter is not None and value.endswith(delimiter.group()):
        return value[len(delimiter.group()) : -len(delimiter.group())]
    if index and stream[index - 1][1].upper() in {"AS", "DO"} and value.startswith("'"):
        return value[1:-1].replace("''", "'")
    return ""


def sql_boundary_violations(source: str) -> set[str]:
    """Reject unsupported SQL constructs, not a growing list of clock names."""
    stream = [
        (kind, value)
        for kind, value in sql_tokens(source)
        if kind not in tokens.Whitespace and kind not in tokens.Comment
    ]
    normalized = " ".join(value for _, value in stream)
    patterns = {
        "runtime-code-generation": r"__clock_runtime_code__",
        "unresolved-statement": r"__clock_unresolved_statement__",
        "operator-ddl": r"\bCREATE\s+(?:OR\s+REPLACE\s+)?OPERATOR\b",
        "event-trigger-ddl": r"\bCREATE\s+EVENT\s+TRIGGER\b",
        "server-prepare": r"\bPREPARE\s+.+?\s+AS\b|\bDEALLOCATE\s+\w+",
        "temporary-ddl": r"\bCREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?TEMP(?:ORARY)?\b",
        "temporary-search-path": (
            r"\bSET\s+(?:(?:LOCAL|SESSION)\s+)?search_path\s*(?:=|TO)\s*"
            r"[\"']?pg_temp(?:_\d+)?\b"
        ),
        "dynamic-keyword": rf"\b(?:CREATE|ALTER|DROP)\s+(?:OR\s+REPLACE\s+)?{DYNAMIC}",
    }
    violations = {
        reason
        for reason, pattern in patterns.items()
        if re.search(pattern, normalized, re.IGNORECASE)
    }
    for index, (_, value) in enumerate(stream):
        violations.update(_word_violations(stream, index))
        body = _nested_body(stream, index)
        if body:
            violations.update(sql_boundary_violations(body))
        if value == DYNAMIC and index + 1 < len(stream) and stream[index + 1][1] == "(":
            prefix = " ".join(
                value.upper() for _, value in stream[max(0, index - 3) : index]
            )
            if "FUNCTION" not in prefix and re.search(
                r"\b(SELECT|PERFORM|RETURN|BEGIN)\b", normalized, re.IGNORECASE
            ):
                violations.add("dynamic-callee")
    return violations


def application_boundary(root: Path) -> dict[str, list[str]]:
    """Scan every apps Python/SQL file, including migration helpers and new files."""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sql"}:
            continue
        source = path.read_text()
        if path.suffix == ".sql":
            fragments = [source]
        else:
            fragments = source_fragments(ast.parse(source))
        violations = set().union(*(sql_boundary_violations(text) for text in fragments))
        if violations:
            found[str(path.relative_to(root))] = sorted(violations)
    return found


def repository_event_trigger_boundary(root: Path) -> dict[str, list[str]]:
    """Refuse event-trigger DDL in executable repo sources, not probe fixtures."""
    found: dict[str, list[str]] = {}
    for directory, folders, names in root.walk():
        folders[:] = [
            name
            for name in folders
            if name not in {"tests", "node_modules", "__pycache__"}
            and (not name.startswith(".") or name == ".github")
        ]
        for name in names:
            path = directory / name
            if path.suffix not in {".py", ".sql", ".sh", ".yml", ".yaml"}:
                continue
            source = path.read_text()
            fragments = (
                source_fragments(ast.parse(source))
                if path.suffix == ".py"
                else [source]
            )
            if any(
                "event-trigger-ddl" in sql_boundary_violations(text)
                for text in fragments
            ):
                found[str(path.relative_to(root))] = ["event-trigger-ddl"]
    return found
