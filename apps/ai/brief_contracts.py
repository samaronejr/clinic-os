"""A closed, citation-only model protocol; no generated assertion can be displayed."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from apps.ai.contracts import GatewayInputError
from apps.core.chart_sources import SECTIONS, ChartSection

type Reference = dict[str, str | int]
type Topic = ChartSection | Literal["brief"]
MAX_QUESTION = 500
MAX_SOURCES = 250
SCHEMA = "chart-request-v1"
REFERENCE_KEYS = {"section", "kind", "id", "version", "date", "digest", "evidence"}
SOURCE_SECTIONS = {
    "encounter": "chronology",
    "problem": "active_problems",
    "problem_assessment": "active_problems",
    "allergy": "allergies",
    "allergy_assessment": "allergies",
    "medication": "medications",
    "observation": "observations",
    "result": "recent_results",
    "task": "outstanding_tasks",
    "note": "prior_decisions",
}
QUESTIONS: dict[str, ChartSection] = {
    "quais problemas estão registrados": "active_problems",
    "quais alergias estão registradas": "allergies",
    "quais medicamentos estão registrados": "medications",
    "quais observações estão registradas": "observations",
    "quais resultados estão registrados": "recent_results",
    "quais tarefas estão pendentes": "outstanding_tasks",
    "quais decisões anteriores estão registradas": "prior_decisions",
    "qual é a cronologia dos atendimentos": "chronology",
    "what problems are recorded": "active_problems",
    "what allergies are recorded": "allergies",
    "what medications are recorded": "medications",
    "what observations are recorded": "observations",
    "what results are recorded": "recent_results",
    "what tasks are outstanding": "outstanding_tasks",
    "what prior decisions are recorded": "prior_decisions",
    "what encounters are recorded": "chronology",
}


def question_topic(question: str) -> ChartSection | None:
    """Unsupported or identity-seeking questions never trigger another retrieval."""
    if type(question) is not str or len(question) > MAX_QUESTION:
        raise GatewayInputError
    return QUESTIONS.get(" ".join(question.strip().rstrip("?").casefold().split()))


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GatewayInputError
        result[key] = value
    return result


def _json(raw: str) -> dict[str, object]:
    try:
        data: object = json.loads(raw, object_pairs_hook=_pairs)
    except (json.JSONDecodeError, RecursionError) as error:
        raise GatewayInputError from error
    if not isinstance(data, dict):
        raise GatewayInputError
    return cast("dict[str, object]", data)


def reference(raw: object) -> Reference:
    """Validate exact citation identities, explicit evidence and canonical dates."""
    if not isinstance(raw, dict) or set(raw) != REFERENCE_KEYS:
        raise GatewayInputError
    data = cast("dict[str, object]", raw)
    kind, section = data["kind"], data["section"]
    version, stamp = data["version"], data["date"]
    identifier, digest, evidence = data["id"], data["digest"], data["evidence"]
    if (
        not isinstance(kind, str)
        or SOURCE_SECTIONS.get(kind) != section
        or type(version) is not int
        or version < 1
        or not isinstance(stamp, str)
        or not isinstance(identifier, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(evidence, str)
        or evidence not in {"recorded", "documented_none"}
        or (evidence == "documented_none" and not kind.endswith("_assessment"))
    ):
        raise GatewayInputError
    try:
        date = datetime.fromisoformat(stamp)
        valid = (
            str(UUID(identifier)) == identifier
            and date.utcoffset() is not None
            and date.isoformat() == stamp
        )
    except ValueError as error:
        raise GatewayInputError from error
    if not valid:
        raise GatewayInputError
    return cast("Reference", data)


@dataclass(frozen=True, slots=True)
class ChartRequest:
    """Only question text is protected content; sources are opaque exact references."""

    enrollment: UUID
    session: str = field(repr=False)
    topic: Topic
    question: str = field(repr=False)
    sources: tuple[Reference, ...]

    def encode(self) -> str:
        """Store one isolated turn, with no previous conversation or chart text."""
        return json.dumps(
            {
                "schema": SCHEMA,
                "enrollment": str(self.enrollment),
                "session": self.session,
                "topic": self.topic,
                "question": self.question,
                "sources": self.sources,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def parse_request(raw: str) -> ChartRequest:
    """Reject unknown keys and any disagreement between the question and topic."""
    data = _json(raw)
    if (
        set(data) != {"schema", "enrollment", "session", "topic", "question", "sources"}
        or data["schema"] != SCHEMA
    ):
        raise GatewayInputError
    enrollment, session = data["enrollment"], data["session"]
    topic, question, sources = data["topic"], data["question"], data["sources"]
    if (
        not isinstance(enrollment, str)
        or not isinstance(session, str)
        or re.fullmatch(r"[0-9a-f]{64}", session) is None
        or not isinstance(question, str)
        or not isinstance(topic, str)
        or topic not in ("brief", *SECTIONS)
        or not isinstance(sources, list)
        or len(sources) > MAX_SOURCES
        or (topic == "brief" and question != "")
        or (topic != "brief" and question_topic(question) != topic)
    ):
        raise GatewayInputError
    try:
        enrolled = UUID(enrollment)
    except ValueError as error:
        raise GatewayInputError from error
    refs = tuple(reference(item) for item in sources)
    if len({json.dumps(item, sort_keys=True) for item in refs}) != len(refs):
        raise GatewayInputError
    return ChartRequest(enrolled, session, cast("Topic", topic), question, refs)


def selected_sources(request: ChartRequest) -> tuple[Reference, ...]:
    """Select cited chart facts without interpreting them or asserting absence."""
    return tuple(
        item
        for item in request.sources
        if request.topic == "brief" or item["section"] == request.topic
    )


def synthetic_reply(raw: str) -> str:
    """Offline protocol simulation, with no clinical text or executable tool."""
    request = parse_request(raw)
    citations = selected_sources(request)
    return json.dumps(
        {
            "schema": "brief-v1",
            "answer": "sources" if citations else "not_found",
            "citations": citations,
        },
        sort_keys=True,
    )


def validate_reply(raw: str, request: ChartRequest) -> tuple[Reference, ...]:
    """Fabricated, cross-patient, omitted or misversioned citations fail closed."""
    data = _json(raw)
    if set(data) != {"schema", "answer", "citations"}:
        raise GatewayInputError
    citations = data["citations"]
    if not isinstance(citations, list):
        raise GatewayInputError
    parsed = tuple(reference(item) for item in citations)
    expected = selected_sources(request)
    if (
        data["schema"] != "brief-v1"
        or data["answer"] != ("sources" if expected else "not_found")
        or parsed != expected
    ):
        raise GatewayInputError
    return parsed
