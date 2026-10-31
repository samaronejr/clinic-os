"""Closed care inputs; every rejection is payload-free (SC-7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from apps.workflows.care import pro_instruments, vocab
from apps.workflows.care.vocab import ADMIN_KINDS, CLINICAL_KINDS
from apps.workflows.validation import WorkflowInputError

__all__ = (
    "ADMIN_KINDS",
    "CLINICAL_KINDS",
    "CareActionSpec",
    "PROSpec",
    "ReferralSpec",
    "due_after_days",
    "return_window",
    "validate_action",
    "validate_pro",
    "validate_referral",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferralSpec:
    """A referral destination; its label is clinical text and stored encrypted."""

    target_kind: str
    target_label: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CareActionSpec:
    """Immutable terms of one typed action; payload fits only its own kind."""

    kind: str
    window_start: datetime
    window_end: datetime
    instruction: str = ""
    referral: ReferralSpec | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PROSpec:
    """A bounded PRO schedule over one available instrument and a human queue."""

    instrument_key: str
    first_due_at: datetime
    interval_days: int
    occurrences: int
    response_role: str
    staffing_policy_ref: str
    plan_id: UUID | None = None


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkflowInputError
    return value


def _bounded(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise WorkflowInputError
    return value


def return_window(*, now: datetime, weeks: int) -> tuple[datetime, datetime]:
    """Return a one-week window that opens ``weeks`` weeks after ``now``."""
    start = _aware(now) + timedelta(weeks=_bounded(weeks, 1, vocab.MAX_RETURN_WEEKS))
    return start, start + timedelta(days=7)


def due_after_days(*, now: datetime, days: int) -> datetime:
    """Return the due instant of a recall; whole days only."""
    return _aware(now) + timedelta(days=_bounded(days, 1, vocab.MAX_RECALL_DAYS))


def validate_referral(spec: object) -> dict[str, str]:
    """Accept a planned referral target; 'unspecified' exists only for backfill."""
    if not isinstance(spec, ReferralSpec):
        raise WorkflowInputError
    label = spec.target_label
    if (
        spec.target_kind not in {"specialty", "external"}
        or not isinstance(label, str)
        or not label.strip()
        or len(label) > vocab.MAX_LABEL
    ):
        raise WorkflowInputError
    return {"target_kind": spec.target_kind, "target_label": label.strip()}


def validate_action(spec: object) -> dict[str, object]:
    """Validate kind, window and the one payload that kind may carry."""
    if not isinstance(spec, CareActionSpec) or spec.kind not in vocab.ACTION_KINDS:
        raise WorkflowInputError
    start, end = _aware(spec.window_start), _aware(spec.window_end)
    if not timedelta(0) < end - start <= timedelta(days=vocab.MAX_WINDOW_DAYS):
        raise WorkflowInputError
    terms: dict[str, object] = {
        "kind": spec.kind,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }
    text = spec.instruction
    if spec.kind == "patient_instruction":
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > vocab.MAX_INSTRUCTION
        ):
            raise WorkflowInputError
        terms["instruction"] = text.strip()
    elif text != "":
        raise WorkflowInputError
    if spec.kind == "referral":
        terms["referral"] = validate_referral(spec.referral)
    elif spec.referral is not None:
        raise WorkflowInputError
    return terms


def validate_pro(spec: object) -> dict[str, object]:
    """Validate an assignable instrument, its schedule and its human queue."""
    if (
        not isinstance(spec, PROSpec)
        or spec.instrument_key not in pro_instruments.available_keys()
        or spec.response_role not in vocab.PRO_RESPONSE_ROLES
        or not isinstance(spec.staffing_policy_ref, str)
        or re.fullmatch(vocab.POLICY_REF_PATTERN, spec.staffing_policy_ref) is None
        or (spec.plan_id is not None and not isinstance(spec.plan_id, UUID))
    ):
        raise WorkflowInputError
    return {
        "instrument_key": spec.instrument_key,
        "instrument_version": pro_instruments.version(spec.instrument_key),
        "first_due_at": _aware(spec.first_due_at).isoformat(),
        "interval_days": _bounded(spec.interval_days, 1, vocab.MAX_PRO_INTERVAL_DAYS),
        "occurrences": _bounded(spec.occurrences, 1, vocab.MAX_PRO_OCCURRENCES),
        "response_role": spec.response_role,
        "staffing_policy_ref": spec.staffing_policy_ref,
        "plan_id": str(spec.plan_id) if spec.plan_id else None,
    }
