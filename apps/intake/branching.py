"""Strict literals and forward-only questionnaire branches, shared with plain JS."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TypedDict, cast

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.core.json_schema import schema_validator

MAX_NUMBER = 9007199254740991
_VALIDATOR = schema_validator(
    Path(__file__).resolve().parents[2]
    / "docs/clinical/questionnaire-branching-schema.json"
)


class Question(TypedDict):
    """A question parsed at publication and retained immutably."""

    id: str
    label: str
    type: str
    required: bool
    max_length: int
    options: list[str]


def definition_parts(
    definition: object,
) -> tuple[list[Question], list[dict[str, object]]]:
    """Read a retained v1 list or a validated v2 envelope."""
    if isinstance(definition, list):
        return cast("list[Question]", definition), []
    data = cast("dict[str, object]", definition)
    return (
        cast("list[Question]", data["questions"]),
        cast("list[dict[str, object]]", data["rules"]),
    )


def is_number(value: object) -> bool:
    """Use finite binary64 numbers within JavaScript's exact integer bounds."""
    return (
        type(value) in (int, float)
        and abs(cast("float", value)) <= MAX_NUMBER
        and math.isfinite(cast("float", value))
    )


def validate_definition(definition: object) -> None:
    """Reject unknown keys, expressions, invalid references and cyclic branches."""
    invalid = _("Invalid questionnaire branching rule.")
    if not _VALIDATOR.is_valid(definition):
        raise ValidationError(invalid)
    questions, rules = definition_parts(definition)
    indices = {question["id"]: index for index, question in enumerate(questions)}
    if len(indices) != len(questions):
        raise ValidationError(invalid)
    by_id = {question["id"]: question for question in questions}
    for rule in rules:
        when = cast("dict[str, object]", rule["when"])
        source = cast("str", when["question"])
        targets = cast("list[str]", rule["show"])
        if source not in indices or any(
            target not in indices or indices[target] <= indices[source]
            for target in targets
        ):
            raise ValidationError(invalid)
        kind = by_id[source]["type"]
        values = when["value"] if when["op"] == "in" else [when["value"]]
        for value in cast("list[object]", values):
            valid = (
                (kind == "number" and is_number(value))
                or (kind == "boolean" and type(value) is bool)
                or (
                    kind in ("text", "selection")
                    and isinstance(value, str)
                    and len(value) <= by_id[source]["max_length"]
                    and (kind != "selection" or value in by_id[source]["options"])
                )
            )
            if not valid or (when["op"] in ("gt", "lt") and kind != "number"):
                raise ValidationError(invalid)


def _equal(left: object, right: object) -> bool:
    """No string/boolean/number coercion; numeric JSON representations agree."""
    if is_number(left) and is_number(right):
        return left == right
    return type(left) is type(right) and left == right


def _matches(when: dict[str, object], answers: dict[str, object]) -> bool:
    source = cast("str", when["question"])
    if source not in answers or answers[source] is None or answers[source] == "":
        return False
    actual = answers[source]
    expected = when["value"]
    match when["op"]:
        case "eq":
            return _equal(actual, expected)
        case "in":
            return any(
                _equal(actual, value) for value in cast("list[object]", expected)
            )
        case "gt":
            return is_number(actual) and cast("float", actual) > cast("float", expected)
        case "lt":
            return is_number(actual) and cast("float", actual) < cast("float", expected)
        case _:
            raise ValidationError(_("Invalid questionnaire branching rule."))


def visible_questions(definition: object, answers: dict[str, object]) -> list[str]:
    """Evaluate in schema order; a hidden source can never reveal a descendant."""
    questions, rules = definition_parts(definition)
    targeted = {key for rule in rules for key in cast("list[str]", rule["show"])}
    visible: list[str] = []
    for question in questions:
        key = question["id"]
        if key not in targeted or any(
            key in cast("list[str]", rule["show"])
            and cast("dict[str, object]", rule["when"])["question"] in visible
            and _matches(cast("dict[str, object]", rule["when"]), answers)
            for rule in rules
        ):
            visible.append(key)
    return visible
