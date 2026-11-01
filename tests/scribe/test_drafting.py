"""Untrusted note output cannot invent facts or cite another patient's source."""

from __future__ import annotations

import json

import pytest
from apps.scribe.draft_contracts import (
    DraftInputError,
    DraftSource,
    parse_suggestions,
    synthetic_reply,
)

REFERENCE: dict[str, str | int] = {
    "kind": "transcript",
    "id": "00000000-0000-0000-0000-000000000001",
    "version": "00000000-0000-0000-0000-000000000002",
    "revision": 1,
    "digest": "a" * 64,
}
SOURCE = DraftSource(REFERENCE, "Synthetic reported symptom.", "reported")


def output(
    *,
    text: str = SOURCE.text,
    citations: list[dict[str, str | int]] | None = None,
    assertion_class: str = "reported",
) -> str:
    return json.dumps(
        {
            "schema": "scribe-suggestions-v1",
            "suggestions": [
                {
                    "section": "subjective",
                    "text": text,
                    "citations": [REFERENCE] if citations is None else citations,
                    "assertion_class": assertion_class,
                }
            ],
            "proposals": [],
        }
    )


def test_factual_suggestion_preserves_its_exact_source_and_base() -> None:
    parsed = parse_suggestions(
        output(),
        sources=(SOURCE,),
        sections=("subjective",),
        base_revision=3,
        generation=4,
    )
    assert len(parsed.sections) == 1
    suggestion = parsed.sections[0]
    assert suggestion.text == SOURCE.text
    assert suggestion.citations == (REFERENCE,)
    assert (suggestion.base_revision, suggestion.generation) == (3, 4)
    assert suggestion.assertion_class == "reported"


def test_uncited_factual_assertion_is_dropped() -> None:
    parsed = parse_suggestions(
        output(citations=[]),
        sources=(SOURCE,),
        sections=("subjective",),
        base_revision=1,
        generation=1,
    )
    assert parsed.sections == ()


@pytest.mark.parametrize(
    "text",
    ["Synthetic unmentioned diagnosis.", "No symptoms.", "Take 15 mg."],
)
def test_citation_is_not_permission_to_invent_diagnosis_negation_or_dose(
    text: str,
) -> None:
    with pytest.raises(DraftInputError):
        parse_suggestions(
            output(text=text),
            sources=(SOURCE,),
            sections=("subjective",),
            base_revision=1,
            generation=1,
        )


def test_foreign_patient_citation_is_rejected_even_with_matching_text() -> None:
    foreign = {**REFERENCE, "id": "00000000-0000-0000-0000-000000000003"}
    with pytest.raises(DraftInputError):
        parse_suggestions(
            output(citations=[foreign]),
            sources=(SOURCE,),
            sections=("subjective",),
            base_revision=1,
            generation=1,
        )


def test_reported_fact_cannot_be_promoted_to_an_observed_finding() -> None:
    with pytest.raises(DraftInputError):
        parse_suggestions(
            output(assertion_class="observed"),
            sources=(SOURCE,),
            sections=("subjective",),
            base_revision=1,
            generation=1,
        )


def test_synthetic_adapter_returns_only_exact_source_linked_drafts() -> None:
    material = json.dumps(
        {
            "schema": "scribe-material-v1",
            "sections": ["subjective", "objective", "assessment"],
            "sources": [
                {
                    "reference": REFERENCE,
                    "text": SOURCE.text,
                    "assertion_class": "reported",
                    "uncertain": False,
                }
            ],
        }
    )
    parsed = parse_suggestions(
        synthetic_reply(material),
        sources=(SOURCE,),
        sections=("subjective", "objective", "assessment"),
        base_revision=1,
        generation=1,
    )
    assert tuple(item.section for item in parsed.sections) == ("subjective",)
    assert parsed.proposals == ()


def encoded_draft(raw: str, encoding: str) -> str:
    """Vary wire bytes without changing the decoded model response."""
    data = json.loads(raw)
    if encoding == "compact":
        return json.dumps(data, separators=(",", ":"))
    if encoding == "pretty":
        return json.dumps(data, indent=2)
    if encoding == "reordered":
        data = dict(reversed(tuple(data.items())))
        data["suggestions"] = [
            dict(reversed(tuple(item.items()))) for item in data["suggestions"]
        ]
        return json.dumps(data)
    assert encoding == "unicode"
    return (
        json.dumps(data)
        .replace('"schema"', r'"\u0073chema"')
        .replace('"citations"', r'"\u0063itations"')
        .replace("No symptoms.", r"\u004eo symptoms.")
    )


@pytest.mark.parametrize("encoding", ["compact", "pretty", "reordered", "unicode"])
@pytest.mark.parametrize("attack", ["fabricated_citation", "forbidden_assertion"])
def test_decoded_draft_validation_cannot_be_skipped_by_json_encoding(
    encoding: str,
    attack: str,
) -> None:
    raw = (
        output(citations=[{**REFERENCE, "id": "00000000-0000-0000-0000-000000000003"}])
        if attack == "fabricated_citation"
        else output(text="No symptoms.")
    )
    with pytest.raises(DraftInputError):
        parse_suggestions(
            encoded_draft(raw, encoding),
            sources=(SOURCE,),
            sections=("subjective",),
            base_revision=1,
            generation=1,
        )
