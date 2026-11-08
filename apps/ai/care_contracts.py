"""Closed care-coordination model contract (AI-05); every item is untrusted output.

The model turns one approved plan section into proposed typed follow-up actions.
Nothing here writes a record: items become reviewable care proposals, and the
deterministic validator below is the only path from model output to a proposal.
Free text, medication, dose and procedure fields have no representable item field.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from apps.ai.contracts import GatewayInputError
from apps.workflows.care.vocab import ACTION_KINDS, ADMIN_KINDS, MAX_WINDOW_DAYS

if TYPE_CHECKING:
    from apps.ai.action_payload import JSON

SCHEMA: Final = "care-coordination-v1"
# Clinician-authored content never comes from the model: a referral destination
# and a patient instruction are free clinical text, so neither is proposable.
CLINICAL_PROPOSABLE: Final = frozenset({"order", "questionnaire", "monitoring_task"})
PROPOSABLE_KINDS: Final = ADMIN_KINDS | CLINICAL_PROPOSABLE
MAX_PLAN_TEXT: Final = 20_000
# One bundle holds at most as many items as a care plan holds actions.
MAX_ITEMS: Final = 16
REASONS: Final = (
    "kind_forbidden",
    "window_invalid",
    "malformed",
    "duplicate",
    "source_superseded",
)
type Refused = Literal[
    "kind_forbidden", "window_invalid", "malformed", "duplicate", "source_superseded"
]
_FIXTURE: Final = Path(__file__).parent / "fixtures" / "care-coordination-v1.json"


@dataclass(frozen=True, slots=True)
class CareRequest:
    """Gateway input: the approved plan section and the version it came from."""

    plan_id: UUID
    plan_revision: int
    source_version_id: UUID
    text: str

    def encode(self) -> str:
        """Serialize the closed request; the parser is the matching boundary."""
        return json.dumps(
            {
                "schema": SCHEMA,
                "plan": str(self.plan_id),
                "revision": self.plan_revision,
                "source": str(self.source_version_id),
                "text": self.text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class CareItem:
    """One validated proposal: a closed kind and a window in days after the plan."""

    position: int
    kind: str
    start_days: int
    end_days: int

    @property
    def audience(self) -> Literal["administrative", "clinical"]:
        """Administrative kinds are staff work; every other kind is clinical."""
        return "administrative" if self.kind in ADMIN_KINDS else "clinical"


@dataclass(frozen=True, slots=True)
class RefusedItem:
    """A model item the validator refused; it never carries model text."""

    position: int
    kind: str
    reason: Refused


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GatewayInputError
        result[key] = value
    return result


def _number(_value: str) -> None:
    raise GatewayInputError


def _load(content: str) -> object:
    try:
        return json.loads(
            content,
            object_pairs_hook=_pairs,
            parse_float=_number,
            parse_constant=_number,
        )
    except (ValueError, RecursionError):
        raise GatewayInputError from None


def parse_request(content: str) -> CareRequest:
    """Accept exactly the request this module encodes; no other key or type."""
    data = _load(content)
    if not isinstance(data, dict) or set(data) != {
        "schema",
        "plan",
        "revision",
        "source",
        "text",
    }:
        raise GatewayInputError
    text, revision = data["text"], data["revision"]
    if (
        data["schema"] != SCHEMA
        or not isinstance(text, str)
        or not 1 <= len(text) <= MAX_PLAN_TEXT
        or type(revision) is not int
        or not 1 <= revision <= 2**31 - 1
    ):
        raise GatewayInputError
    try:
        plan, source = UUID(str(data["plan"])), UUID(str(data["source"]))
    except ValueError:
        raise GatewayInputError from None
    if str(plan) != data["plan"] or str(source) != data["source"]:
        raise GatewayInputError
    return CareRequest(plan, revision, source, text)


def _stored_kind(value: object) -> str:
    """Record only a closed kind; an invented model kind becomes 'unsupported'."""
    return value if isinstance(value, str) and value in ACTION_KINDS else "unsupported"


def _item(
    position: int, raw: object, seen: set[tuple[str, int, int]]
) -> CareItem | RefusedItem:
    if not isinstance(raw, dict) or set(raw) != {"kind", "start_days", "end_days"}:
        kind = raw.get("kind") if isinstance(raw, dict) else None
        stored = _stored_kind(kind)
        # Extra fields (a dose, a drug, free text) are refused with the kind.
        reason: Refused = (
            "kind_forbidden" if stored not in PROPOSABLE_KINDS else "malformed"
        )
        return RefusedItem(position, stored, reason)
    kind, start, end = raw["kind"], raw["start_days"], raw["end_days"]
    stored = _stored_kind(kind)
    if stored not in PROPOSABLE_KINDS:
        return RefusedItem(position, stored, "kind_forbidden")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= MAX_WINDOW_DAYS
    ):
        return RefusedItem(position, stored, "window_invalid")
    if (stored, start, end) in seen:
        return RefusedItem(position, stored, "duplicate")
    seen.add((stored, start, end))
    return CareItem(position, stored, start, end)


def validate_reply(
    content: str,
) -> tuple[tuple[CareItem, ...], tuple[RefusedItem, ...]]:
    """Deterministically split model items into proposals and refusals.

    The envelope must be exactly ``{"schema", "items"}``; anything else is an
    invalid output for the whole bundle. Each item is judged on its own, so one
    invalid item never hides the valid ones and never becomes a proposal.
    """
    data = _load(content)
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "items"}
        or data["schema"] != SCHEMA
        or not isinstance(data["items"], list)
    ):
        raise GatewayInputError
    if len(data["items"]) > MAX_ITEMS:
        raise GatewayInputError
    accepted: list[CareItem] = []
    refused: list[RefusedItem] = []
    seen: set[tuple[str, int, int]] = set()
    for position, raw in enumerate(data["items"]):
        item = _item(position, raw, seen)
        if isinstance(item, CareItem):
            accepted.append(item)
        else:
            refused.append(item)
    return tuple(accepted), tuple(refused)


def _fixture_items(text: str) -> list[JSON]:
    """Replay the reviewed synthetic recording for this exact plan section."""
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "data_class", "responses"}
        or payload["schema_version"] != 1
        or payload["data_class"] != "synthetic"
        or not isinstance(payload["responses"], list)
    ):
        raise GatewayInputError
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    for response in payload["responses"]:
        if (
            isinstance(response, dict)
            and set(response) == {"label", "text_sha256", "items"}
            and response["text_sha256"] == digest
            and isinstance(response["items"], list)
        ):
            items: list[JSON] = response["items"]
            return items
    return []


def synthetic_reply(content: str) -> str:
    """Replay reviewed fixtures only; any other plan text proposes nothing."""
    request = parse_request(content)
    return json.dumps(
        {"schema": SCHEMA, "items": _fixture_items(request.text)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
