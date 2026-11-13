"""The latest recorded synthetic recovery exercise, for the governance screen.

Todo 72 records each passing local exercise (``python -m
ops.testing.restore_rehearsal_acceptance``) as ``docs/operations/
recovery-exercise.json``. The record is platform evidence at level L1: it is
read, validated against its closed schema and summarized here, and it never
approves recovery, provider PITR or live operation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from pathlib import Path
from typing import Final, Literal, Protocol

_DOCS: Final = Path(__file__).resolve().parents[2] / "docs" / "operations"
RECORD_PATH: Final = _DOCS / "recovery-exercise.json"
SCHEMA_PATH: Final = _DOCS / "recovery-exercise.schema.json"


@dataclass(frozen=True, slots=True)
class RecoveryExercise:
    """What the screen may say about the latest recorded exercise."""

    state: Literal["recorded", "absent", "unreadable"]
    finished_on: str = ""
    revision: str = ""
    rpo_seconds: float = 0.0
    rpo_proposal_seconds: int = 0
    rto_seconds: float = 0.0
    rto_proposal_seconds: int = 0
    attachment_objects: int = 0
    audit_events: int = 0
    tenant_key_versions: int = 0


class _Validator(Protocol):
    def validate(self, instance: object) -> None: ...


@cache
def _validator() -> _Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator: _Validator = import_module("jsonschema").Draft202012Validator(schema)
    return validator


def latest_recovery_exercise(path: Path | None = None) -> RecoveryExercise:
    """Summarize the recorded exercise; a missing or invalid record says so."""
    try:
        raw = (RECORD_PATH if path is None else path).read_bytes()
    except FileNotFoundError:
        return RecoveryExercise("absent")
    except OSError:
        return RecoveryExercise("unreadable")
    try:
        record = json.loads(raw)
        _validator().validate(record)
    except (ValueError, import_module("jsonschema").ValidationError):
        return RecoveryExercise("unreadable")
    drill = record["drill"]
    objectives = drill["recovery_objectives"]
    return RecoveryExercise(
        "recorded",
        finished_on=record["finished_at"][:10],
        revision=record["source_revision"]["commit"][:12],
        rpo_seconds=objectives["rpo_measured_seconds"],
        rpo_proposal_seconds=objectives["rpo_proposal_seconds"],
        rto_seconds=objectives["rto_measured_seconds"],
        rto_proposal_seconds=objectives["rto_proposal_seconds"],
        attachment_objects=drill["attachment_objects_verified"],
        audit_events=drill["audit_content_verified"],
        tenant_key_versions=len(drill["tenant_key_status"].splitlines()),
    )
