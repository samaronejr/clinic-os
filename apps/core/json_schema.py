"""Typed boundary to the installed JSON Schema implementation."""

from __future__ import annotations

import json
from importlib import import_module
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class SchemaValidator(Protocol):
    """The closed validation operation used by declarative definitions."""

    def is_valid(self, instance: object) -> bool:
        """Whether a definition satisfies its shipped schema."""
        ...


def schema_validator(path: Path) -> SchemaValidator:
    """Load a local Draft 2020-12 schema with format checks, never remote refs."""
    library = import_module("jsonschema")
    validator: SchemaValidator = library.Draft202012Validator(
        json.loads(path.read_text()), format_checker=library.FormatChecker()
    )
    return validator
