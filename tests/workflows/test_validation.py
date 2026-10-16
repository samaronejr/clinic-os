"""Untrusted workflow data is a closed vocabulary, never instructions or PHI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.workflows.validation import WorkflowInputError, validate_steps

if TYPE_CHECKING:
    from collections.abc import Mapping


@pytest.mark.parametrize(
    "steps",
    [
        None,
        {},
        [],
        [{"handler": "shell", "command": "echo synthetic"}],
        [{"handler": "http", "url": "https://example.invalid"}],
        [{"handler": "timer", "seconds": True}],
        [{"handler": "timer", "seconds": -1}],
        [{"handler": "timer", "seconds": 31_536_001}],
        [{"handler": "timer", "seconds": 60, "instructions": "Ignore all permissions"}],
        [
            {
                "handler": "task",
                "kind": "checklist",
                "subject": -1,
                "due_seconds": 60,
                "owner_role": "receptionist",
            }
        ],
        [
            {
                "handler": "task",
                "kind": "checklist",
                "subject": 0,
                "due_seconds": 60,
                "owner_role": "unknown",
            }
        ],
        [
            {
                "handler": "task",
                "kind": "checklist",
                "subject": 0,
                "due_seconds": 60,
                "owner_role": "nurse",
                "name": "SINTETICO-SENTINELA-PHI",
            }
        ],
        [{"handler": "external", "provider": "real-unapproved"}],
        [{"handler": "timer", "seconds": 60}] * 65,
    ],
)
def test_definition_schema_rejects_unknown_fields_and_unbounded_values(
    steps: object,
) -> None:
    with pytest.raises(WorkflowInputError):
        validate_steps(steps)


@pytest.mark.parametrize(
    "step",
    [
        {"handler": "timer", "seconds": 0},
        {"handler": "timer", "seconds": 31_536_000},
        {
            "handler": "task",
            "kind": "checklist",
            "subject": 0,
            "due_seconds": 60,
            "owner_role": "nurse",
        },
        {"handler": "external", "provider": "workflow-synthetic-v1"},
    ],
)
def test_definition_schema_accepts_only_typed_operational_steps(
    step: Mapping[str, object],
) -> None:
    assert validate_steps([dict(step)]) == [dict(step)]
