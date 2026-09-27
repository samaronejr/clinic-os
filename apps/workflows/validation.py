"""Closed, reference-only schemas at the workflow input boundary."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import rfc8785
from django.utils import timezone

from apps.identity.models import UserClinicRole

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.audit.canonical import CanonicalValue

KINDS: Final = frozenset({"checklist", "review", "follow_up"})
PRIORITIES: Final = frozenset({"low", "normal", "high", "urgent"})
REF_KINDS: Final = frozenset({"clinic", "task", "enrollment", "appointment"})
MAX_STEPS: Final = 64
MAX_SECONDS: Final = 31_536_000


class WorkflowInputError(ValueError):
    """Reject malformed values without reflecting untrusted text."""

    def __init__(self) -> None:
        """Keep exception text out of the clinical data channel."""
        super().__init__("workflow input is invalid")


class WorkflowConflictError(ValueError):
    """Report stale/replayed commands without exposing the competing record."""

    def __init__(self) -> None:
        """Keep every conflict payload-free."""
        super().__init__("workflow command conflicts with current state")


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskOwner:
    """The proposed owner is a target, never the acting principal."""

    user_id: UUID | None = None
    role: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskSpec:
    """Immutable creation terms for a typed operational task."""

    kind: str
    subject_ref: Mapping[str, object]
    due_at: datetime
    priority: str = "normal"
    depends_on: UUID | None = None
    escalation_policy_version: int = 1


def digest(value: object) -> str:
    """Bind already-validated JSON using RFC 8785, never repr or float coercion."""
    return hashlib.sha256(rfc8785.dumps(cast("CanonicalValue", value))).hexdigest()


def reference(value: object) -> dict[str, str]:
    """Accept exactly a known kind and a canonical UUID, never embedded content."""
    if not isinstance(value, dict) or set(value) != {"kind", "id"}:
        raise WorkflowInputError
    kind, identifier = value["kind"], value["id"]
    if (
        not isinstance(kind, str)
        or kind not in REF_KINDS
        or not isinstance(identifier, str)
    ):
        raise WorkflowInputError
    try:
        parsed = UUID(identifier)
    except ValueError as error:
        raise WorkflowInputError from error
    if str(parsed) != identifier:
        raise WorkflowInputError
    return {"kind": kind, "id": identifier}


def validate_task(spec: TaskSpec) -> dict[str, object]:
    """Validate all creation terms before any write or audit append."""
    if (
        spec.kind not in KINDS
        or spec.priority not in PRIORITIES
        or type(spec.escalation_policy_version) is not int
        or spec.escalation_policy_version != 1
        or not isinstance(spec.due_at, datetime)
        or timezone.is_naive(spec.due_at)
        or (spec.depends_on is not None and not isinstance(spec.depends_on, UUID))
    ):
        raise WorkflowInputError
    return {
        "kind": spec.kind,
        "subject_ref": reference(dict(spec.subject_ref)),
        "due_at": spec.due_at.isoformat(),
        "priority": spec.priority,
        "depends_on": str(spec.depends_on) if spec.depends_on else None,
        "escalation_policy_version": spec.escalation_policy_version,
    }


def validate_owner(owner: TaskOwner) -> None:
    """Exactly one owner is required; roles remain canonical stored values."""
    if not (
        (isinstance(owner.user_id, UUID) and owner.role == "")
        or (owner.user_id is None and owner.role in UserClinicRole.Role.values)
    ):
        raise WorkflowInputError


def evidence(kind: str, value: object) -> dict[str, object]:
    """Validate kind-specific evidence; no free-text completion payload exists."""
    if not isinstance(value, dict):
        raise WorkflowInputError
    if kind == "checklist":
        if set(value) == {"checked"} and value["checked"] is True:
            return {"checked": True}
    elif (
        kind in {"review", "follow_up"}
        and set(value) == {"record", "outcome"}
        and value["outcome"] in ("reviewed", "resolved", "escalated")
    ):
        return {"record": reference(value["record"]), "outcome": value["outcome"]}
    raise WorkflowInputError


def machine_key(value: str) -> str:
    """Only bounded machine slugs can identify a recurring definition."""
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        raise WorkflowInputError
    return value


def _bounded_int(value: object, upper: int) -> bool:
    return type(value) is int and 0 <= value <= upper


def _valid_step(step: object) -> bool:
    if not isinstance(step, dict):
        return False
    handler = step.get("handler")
    if handler == "timer":
        return set(step) == {"handler", "seconds"} and _bounded_int(
            step["seconds"], MAX_SECONDS
        )
    if handler == "task":
        return (
            set(step) == {"handler", "kind", "subject", "due_seconds", "owner_role"}
            and isinstance(step["kind"], str)
            and step["kind"] in KINDS
            and _bounded_int(step["subject"], MAX_STEPS - 1)
            and _bounded_int(step["due_seconds"], MAX_SECONDS)
            and step["owner_role"] in UserClinicRole.Role.values
        )
    return handler == "external" and step == {
        "handler": "external",
        "provider": "workflow-synthetic-v1",
    }


def validate_steps(value: object) -> list[dict[str, object]]:
    """Validate version one's operational tasks, timers and synthetic adapter."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_STEPS:
        raise WorkflowInputError
    if not all(_valid_step(step) for step in value):
        raise WorkflowInputError
    return [dict(step) for step in value]
