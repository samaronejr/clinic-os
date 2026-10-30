"""Closed decision-support output: exact facts, licensed sources, bounded hypotheses."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID

from apps.ai.brief_contracts import ChartRequest, parse_request, reference
from apps.ai.contracts import MAX_CONTENT_BYTES, GatewayInputError
from apps.ai.reference_catalog import MAX_CATALOG_ENTRIES, MAX_CATALOG_TEXT

if TYPE_CHECKING:
    from apps.ai.brief_contracts import Reference
    from apps.ai.reference_catalog import CatalogEntry

SCHEMA = "decision-support-request-v1"
OUTPUT_SCHEMA = "decision-support-v1"
KINDS = ("review_concordance", "review_completeness")
MAX_REFERENCE_KEY = 100
MAX_HYPOTHESES = 25


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise GatewayInputError
        data[key] = value
    return data


def _object(raw: str) -> dict[str, object]:
    if len(raw.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise GatewayInputError
    try:
        data: object = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as error:
        raise GatewayInputError from error
    if not isinstance(data, dict):
        raise GatewayInputError
    return cast("dict[str, object]", data)


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Each clinician opts in for this one encounter and session, never globally."""

    encounter: UUID
    chart: ChartRequest
    references: tuple[dict[str, str | int], ...]

    def encode(self) -> str:
        """Encode exact references without caller-selected instructions or tools."""
        return json.dumps(
            {
                "schema": SCHEMA,
                "encounter": str(self.encounter),
                "opt_in": True,
                "chart": json.loads(self.chart.encode()),
                "references": self.references,
            },
            sort_keys=True,
        )


def parse_decision_request(raw: str) -> DecisionRequest:
    """Reject missing opt-in, open instructions and ambiguous or duplicate keys."""
    data = _object(raw)
    if (
        set(data) != {"schema", "encounter", "opt_in", "chart", "references"}
        or data["schema"] != SCHEMA
        or data["opt_in"] is not True
        or not isinstance(data["encounter"], str)
        or not isinstance(data["references"], list)
        or not 1 <= len(data["references"]) <= MAX_CATALOG_ENTRIES
    ):
        raise GatewayInputError
    try:
        encounter = UUID(data["encounter"])
    except ValueError as error:
        raise GatewayInputError from error
    chart = parse_request(json.dumps(data["chart"]))
    if chart.topic != "brief":
        raise GatewayInputError
    refs: list[dict[str, str | int]] = []
    for item in data["references"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "key",
                "version",
                "digest",
                "source",
                "published_on",
                "population",
                "statement",
                "limitations",
                "license_ref",
            }
            or not isinstance(item["key"], str)
            or not 1 <= len(item["key"]) <= MAX_REFERENCE_KEY
            or type(item["version"]) is not int
            or item["version"] < 1
            or not isinstance(item["digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", item["digest"]) is None
            or any(
                not isinstance(value, str) or not value or len(value) > MAX_CATALOG_TEXT
                for key, value in item.items()
                if key != "version"
            )
        ):
            raise GatewayInputError
        refs.append(cast("dict[str, str | int]", item))
    if len({item["key"] for item in refs}) != len(refs):
        raise GatewayInputError
    return DecisionRequest(encounter, chart, tuple(refs))


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """No arbitrary text can smuggle a diagnosis, prognosis, prescription or dose."""

    kind: str
    fact: Reference
    citation: dict[str, str | int]


def validate_decision_reply(
    raw: str, request: DecisionRequest
) -> tuple[Hypothesis, ...]:
    """Drop a fabricated citation's entire response before storing any output."""
    data = _object(raw)
    if (
        set(data)
        != {"schema", "patient_facts", "reference_statements", "model_hypotheses"}
        or data["schema"] != OUTPUT_SCHEMA
        or json.dumps(data["patient_facts"], sort_keys=True)
        != json.dumps(request.chart.sources, sort_keys=True)
        or json.dumps(data["reference_statements"], sort_keys=True)
        != json.dumps(request.references, sort_keys=True)
        or not isinstance(data["model_hypotheses"], list)
        or len(data["model_hypotheses"]) > MAX_HYPOTHESES
    ):
        raise GatewayInputError
    hypotheses: list[Hypothesis] = []
    for item in data["model_hypotheses"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "fact", "reference"}
            or item["kind"] not in KINDS
            or item["fact"] not in request.chart.sources
            or json.dumps(item["reference"], sort_keys=True)
            not in {json.dumps(entry, sort_keys=True) for entry in request.references}
        ):
            raise GatewayInputError
        hypothesis = Hypothesis(
            item["kind"],
            reference(item["fact"]),
            cast("dict[str, str | int]", item["reference"]),
        )
        if hypothesis in hypotheses:
            raise GatewayInputError
        hypotheses.append(hypothesis)
    return tuple(hypotheses)


def synthetic_decision_reply(raw: str) -> str:
    """Select evidence-review considerations without producing clinical advice."""
    request = parse_decision_request(raw)
    return json.dumps(
        {
            "schema": OUTPUT_SCHEMA,
            "patient_facts": request.chart.sources,
            "reference_statements": request.references,
            "model_hypotheses": [
                {
                    "kind": "review_concordance",
                    "fact": fact,
                    "reference": request.references[0],
                }
                for fact in request.chart.sources[:MAX_HYPOTHESES]
            ],
        },
        sort_keys=True,
    )


def resolve_catalog(
    request: DecisionRequest, catalog: tuple[CatalogEntry, ...]
) -> None:
    """Invalidate a turn when any licensed reference content or metadata changes."""
    if request.references != tuple(entry.reference() for entry in catalog):
        raise GatewayInputError
