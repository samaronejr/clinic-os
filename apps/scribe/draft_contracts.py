"""Closed source-exact drafts; citations never license invented clinical facts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.brief_contracts import Reference

SECTIONS = ("subjective", "objective", "assessment", "plan")
MAX_TEXT = 20_000
MAX_SOURCES = 250
MAX_OUTPUT = 1_048_576
MAX_CITATIONS = 8
type AssertionClass = Literal["reported", "observed", "hypothesis"]
type ProposalKind = Literal["order", "task"]
type Decision = Literal["accepted", "modified", "rejected"]


class DraftReason(StrEnum):
    """Closed command-conflict reasons contain no clinical content."""

    STALE = "stale_revision"
    SOURCE = "source_superseded"
    GENERATION = "generation_superseded"
    SECTION = "section_conflict"
    REPLAY = "idempotency_mismatch"
    REVIEWED = "already_reviewed"
    PENDING = "transcript_pending"
    CAPACITY = "source_capacity"


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """Revision and section selection are one generation's requested terms."""

    expected_revision: int
    sections: tuple[str, ...] = SECTIONS


@dataclass(frozen=True, slots=True)
class ReviewInput:
    """A bounded, idempotent clinician command, never caller-supplied authority."""

    suggestion_id: UUID
    expected_revision: int
    command_id: UUID
    action: Decision
    content: dict[str, str] = field(repr=False)
    replacement: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Reject invalid actions and require an explicit modified replacement."""
        if (
            type(self.expected_revision) is not int
            or self.expected_revision < 1
            or self.action not in {"accepted", "modified", "rejected"}
        ) or (
            self.action == "modified"
            and (type(self.replacement) is not str or len(self.replacement) > MAX_TEXT)
        ):
            raise DraftInputError


class DraftInputError(ValueError):
    """Never echo a model response, clinical text or record selector."""

    def __init__(self) -> None:
        """Expose one content-free boundary failure."""
        super().__init__("invalid note suggestion")


class DraftAccessDeniedError(PermissionError):
    """Unknown, foreign, inactive and unassigned references deny identically."""

    def __init__(self) -> None:
        """Keep selectors out of exception text."""
        super().__init__("note suggestion access denied")


class DraftConflictError(RuntimeError):
    """A refusal is a command result, never a persisted document state."""

    def __init__(self, reason: DraftReason = DraftReason.STALE) -> None:
        """Carry only a closed internal reason code."""
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class DraftSource:
    """Ephemeral text from an owning-domain service, never a broker argument."""

    reference: Reference
    text: str = field(repr=False)
    assertion_class: AssertionClass
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class SectionSuggestion:
    """The review DTO retains both note and model generation provenance."""

    section: str
    text: str = field(repr=False)
    citations: tuple[Reference, ...]
    assertion_class: AssertionClass
    base_revision: int
    generation: int


@dataclass(frozen=True, slots=True)
class ActionSuggestion:
    """An A0 proposal, never note text or permission to execute a domain effect."""

    kind: ProposalKind
    text: str = field(repr=False)
    citations: tuple[Reference, ...]


@dataclass(frozen=True, slots=True)
class DraftOutput:
    """Clinical orders and tasks have a distinct, non-applicable collection."""

    sections: tuple[SectionSuggestion, ...]
    proposals: tuple[ActionSuggestion, ...]


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DraftInputError
        result[key] = value
    return result


def document(raw: str) -> dict[str, object]:
    """Bound and parse untrusted JSON once, including duplicate-key refusal."""
    if type(raw) is not str or len(raw.encode("utf-8")) > MAX_OUTPUT:
        raise DraftInputError
    try:
        value: object = json.loads(raw, object_pairs_hook=_pairs)
    except (json.JSONDecodeError, RecursionError) as error:
        raise DraftInputError from error
    if not isinstance(value, dict):
        raise DraftInputError
    return cast("dict[str, object]", value)


def _supported(
    data: dict[str, object], sources: tuple[DraftSource, ...]
) -> tuple[str, tuple[Reference, ...], AssertionClass] | None:
    text, refs = data["text"], data["citations"]
    if (
        not isinstance(text, str)
        or not text
        or len(text) > MAX_TEXT
        or not isinstance(refs, list)
        or len(refs) > MAX_CITATIONS
    ):
        raise DraftInputError
    if not refs:
        return None
    chosen: list[DraftSource] = []
    for reference in refs:
        source = next((item for item in sources if item.reference == reference), None)
        if source is None or source in chosen:
            raise DraftInputError
        chosen.append(source)
    # Extraction, not clinical invention: even a valid citation cannot support
    # an altered negation, dose or diagnosis. More fluent live generation is a
    # separately evaluated contract, not an implicit relaxation of this one.
    if text != "\n".join(item.text for item in chosen):
        raise DraftInputError
    classes = {item.assertion_class for item in chosen}
    assertion: AssertionClass = (
        chosen[0].assertion_class
        if len(classes) == 1 and not any(item.uncertain for item in chosen)
        else "hypothesis"
    )
    return text, tuple(item.reference for item in chosen), assertion


def parse_suggestions(
    raw: str,
    *,
    sources: tuple[DraftSource, ...],
    sections: tuple[str, ...],
    base_revision: int,
    generation: int,
) -> DraftOutput:
    """Validate the entire result before persisting any suggestion or proposal."""
    data = document(raw)
    suggestions, proposals = data.get("suggestions"), data.get("proposals")
    if (
        set(data) != {"schema", "suggestions", "proposals"}
        or data["schema"] != "scribe-suggestions-v1"
        or not isinstance(suggestions, list)
        or len(suggestions) > len(SECTIONS)
        or not isinstance(proposals, list)
        or len(proposals) > MAX_CITATIONS
        or not sections
        or not set(sections) <= set(SECTIONS)
        or len(set(sections)) != len(sections)
        or len(sources) > MAX_SOURCES
    ):
        raise DraftInputError
    result: list[SectionSuggestion] = []
    seen: set[str] = set()
    for raw_item in suggestions:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "section",
            "text",
            "citations",
            "assertion_class",
        }:
            raise DraftInputError
        item = cast("dict[str, object]", raw_item)
        section = item["section"]
        if not isinstance(section, str) or section not in sections or section in seen:
            raise DraftInputError
        seen.add(section)
        supported = _supported(item, sources)
        if supported is None:
            continue
        text, citations, assertion = supported
        if item["assertion_class"] != assertion:
            raise DraftInputError
        result.append(
            SectionSuggestion(
                section, text, citations, assertion, base_revision, generation
            )
        )
    return DraftOutput(tuple(result), _proposals(proposals, sources))


def _proposals(
    proposals: list[object], sources: tuple[DraftSource, ...]
) -> tuple[ActionSuggestion, ...]:
    actions: list[ActionSuggestion] = []
    for raw_item in proposals:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "kind",
            "text",
            "citations",
        }:
            raise DraftInputError
        item = cast("dict[str, object]", raw_item)
        kind = item["kind"]
        if kind not in ("order", "task"):
            raise DraftInputError
        supported = _supported(item, sources)
        if supported is not None:
            text, citations, _assertion = supported
            actions.append(ActionSuggestion(kind, text, citations))
    return tuple(actions)


def synthetic_reply(raw: str) -> str:
    """Extract only exact synthetic source text with the runnable adapter."""
    data = document(raw)
    if data.get("schema") != "scribe-material-v1":
        raise DraftInputError
    raw_sources, sections = data.get("sources"), data.get("sections")
    if not isinstance(raw_sources, list) or not isinstance(sections, list):
        raise DraftInputError
    sources: list[dict[str, object]] = []
    for source in raw_sources:
        if not isinstance(source, dict):
            raise DraftInputError
        sources.append(cast("dict[str, object]", source))
    suggestions: list[dict[str, object]] = []
    used: set[int] = set()
    for section in sections:
        eligible = [
            (index, source)
            for index, source in enumerate(sources)
            if index not in used
            and (
                (section == "subjective" and source["assertion_class"] == "reported")
                or (section == "objective" and source["assertion_class"] == "observed")
                or (
                    section == "assessment"
                    and source["assertion_class"] == "hypothesis"
                )
                or section == "plan"
            )
        ]
        if not eligible:
            continue
        index, source = eligible[0]
        used.add(index)
        suggestions.append(
            {
                "section": section,
                "text": source["text"],
                "citations": [source["reference"]],
                "assertion_class": (
                    "hypothesis" if source["uncertain"] else source["assertion_class"]
                ),
            }
        )
    return json.dumps(
        {
            "schema": "scribe-suggestions-v1",
            "suggestions": suggestions,
            "proposals": [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
