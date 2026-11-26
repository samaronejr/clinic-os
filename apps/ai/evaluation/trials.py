"""Purpose-bound offline trials. Domain parsers, not the evaluator, interpret output."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from django.utils.translation import override

from apps.ai import (
    brief_contracts,
    care_contracts,
    decision_contracts,
    extraction_contracts,
    front_desk,
    revenue_contracts,
)
from apps.ai.contracts import GatewayInputError, Method, Purpose
from apps.ai.evaluation.corpus import Case, case_audio
from apps.ai.evaluation.origin import DOMAIN_PROTOCOL
from apps.ai.evaluation.protocol import EvaluationInputError
from apps.ai.extraction_documents import document_input, source_regions
from apps.ai.front_desk_kb import ArticleAnswer
from apps.ai.reference_catalog import load_catalog
from apps.analytics import analyst_contracts
from apps.analytics.contracts import descriptor, parse_descriptor
from apps.core.chart_sources import ChartSource
from apps.scribe import asr, draft_contracts

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.ai.extraction_contracts import SourceEvidence

EPOCH = datetime(2031, 1, 1, tzinfo=UTC)
ASR_DURATION_MS = 5_000
DEPLETED_LINK_CASE = 10


@dataclass(frozen=True)
class Trial:
    """A single domain request and a projection against its independent facts."""

    content: str
    method: Method
    observe: Callable[[str], bool]


def transcript(output: str) -> str:
    """Measure WER only on parsed final recognition text, never batch JSON."""
    final = asr.parse_batch(output, duration_ms=ASR_DURATION_MS)[-1]
    return (
        " ".join(segment.text for segment in final.segments) if final.is_final else ""
    )


def _draft(source: draft_contracts.DraftSource, output: str) -> bool:
    parsed = draft_contracts.parse_suggestions(
        output,
        sources=(source,),
        sections=("subjective",),
        base_revision=1,
        generation=1,
    )
    return (
        len(parsed.sections) == 1
        and parsed.sections[0].section == "subjective"
        and parsed.sections[0].citations == (source.reference,)
        and not parsed.proposals
    )


def _extraction(
    regions: SourceEvidence, expected: list[list[str]], output: str
) -> bool:
    rows = extraction_contracts.parse_proposals(output, rotations=(0,), regions=regions)
    return [[r.field, r.status, r.parsed_value, r.unit] for r in rows] == expected


def _document(provider: str, model: str) -> Trial:
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/document_extraction_v1.json"
        ).read_bytes()
    )
    data = base64.b64decode(fixture["data_base64"], validate=True)
    policy = json.loads(DOMAIN_PROTOCOL.read_bytes())["extraction"]
    if sha256(data).hexdigest() != policy["source_sha256"]:
        raise EvaluationInputError
    mode = model if provider == "recorded_fixture" else provider
    if mode not in policy or mode not in {"synthetic", "extraction-fixture-v1"}:
        raise GatewayInputError
    return Trial(
        document_input(data=data, content_type="image/png"),
        "extract",
        partial(
            _extraction,
            source_regions(data=data, content_type="image/png", rotations=(0,)),
            policy[mode],
        ),
    )


def _care(index: int, identity: UUID) -> Trial:
    scenarios = json.loads(DOMAIN_PROTOCOL.read_bytes())["care_coordination"]
    scenario = scenarios[index % len(scenarios)]
    request = care_contracts.CareRequest(identity, 1, identity, scenario["text"])

    def observe(output: str) -> bool:
        accepted, refused = care_contracts.validate_reply(output, request)
        return (list(scenario["accepted"]), list(scenario["refused"])) == (
            [item.kind for item in accepted],
            [item.reason for item in refused],
        )

    return Trial(request.encode(), "complete", observe)


def _revenue(index: int) -> Trial:
    total = 12345 + index
    denied = 4000 if index % 2 == 0 else 0
    facts = revenue_contracts.RevenueFacts(
        {
            "source": "G1",
            "total_minor": total,
            "procedures": [
                {"table": "22", "code": "10101012", "quantity": 1, "unit_minor": total}
            ],
        },
        {"source": "J1", "net_minor": total, "recognized": True},
        (
            {
                "source": "R1",
                "sequence": 1,
                "charged_minor": total,
                "accepted_minor": total - denied,
                "denied_minor": denied,
                "code": "1701" if denied else "",
            },
        ),
        {"source": "P1", "allocated_minor": 0},
    )
    return Trial(
        facts.encode(),
        "complete",
        lambda output: revenue_contracts.validate_reply(output, facts) is not None,
    )


def front_desk_case(index: int, case: Case) -> tuple[str, bool]:
    """Compare an isolated production routing decision, without delivering it."""
    spec = json.loads(DOMAIN_PROTOCOL.read_bytes())["front_desk"]
    text, intent, outcome, tool, handoff = spec["cases"][index]
    identity = uuid5(NAMESPACE_URL, "clinic-os-evaluation:" + case["id"])
    article = ArticleAnswer(
        identity,
        spec["article"]["key"],
        1,
        "Sintético",
        spec["article"]["body"],
        tuple(spec["article"]["keywords"]),
        spec["article"]["link"],
    )
    ctx = front_desk.Context(
        turn_id=identity,
        channel="portal",
        thread_state="active",
        verified=False,
        session_id=None,
        challenge_pending=False,
        attempts=0,
        offered_appointment=None,
        offered_practitioner=None,
        offered_start=None,
        first_turn=False,
        text=text,
        tools=("comms.send_approved_link",) if index in {1, DEPLETED_LINK_CASE} else (),
        budget_left=0 if index == DEPLETED_LINK_CASE else 1,
        offer_received=False,
    )
    with override("pt-br"):
        decision = front_desk.route_message(ctx, lambda: (article,))
    expected_article = (
        identity if (intent, outcome) == ("information", "answered") else None
    )
    observed = (
        decision.intent,
        decision.outcome,
        decision.tool,
        decision.handoff,
        decision.article,
    )
    valid = observed == (intent, outcome, tool, handoff, expected_article) and (
        decision.reply == article.body + ("\n" + article.link if tool else "")
        if expected_article
        else bool(decision.reply)
    )
    return json.dumps(
        {
            "intent": decision.intent,
            "outcome": decision.outcome,
            "tool": decision.tool,
            "handoff": decision.handoff,
            "reply": decision.reply,
        },
        ensure_ascii=False,
        sort_keys=True,
    ), valid


def trial(case: Case, index: int, purpose: Purpose, provider: str, model: str) -> Trial:
    """Choose the production parser by requested purpose, never by provider output."""
    identity = uuid5(NAMESPACE_URL, "clinic-os-evaluation:" + case["id"])
    chart_source = ChartSource(
        "observations", "observation", identity, 1, EPOCH, case["script"]
    )
    chart = brief_contracts.ChartRequest(
        identity, "1" * 64, "brief", "", (chart_source.reference(),)
    )
    if purpose in {
        Purpose.EXTRACTION,
        Purpose.CARE_COORDINATION,
        Purpose.REVENUE_CYCLE,
    }:
        return _administrative_trial(index, identity, purpose, provider, model)
    match purpose:
        case Purpose.SCRIBE_ASR:
            audio = asr.AudioInput(
                identity,
                identity,
                "audio/ogg;codecs=opus",
                ASR_DURATION_MS,
                case_audio(case),
            )
            return Trial(
                audio.encode(), "transcribe", lambda output: bool(transcript(output))
            )
        case Purpose.SCRIBE_DRAFT:
            source = draft_contracts.DraftSource(
                chart_source.reference(), case["script"], "reported"
            )
            return Trial(
                draft_contracts.draft_material(("subjective",), (source,)),
                "complete",
                partial(_draft, source),
            )
        case Purpose.BRIEF:
            return Trial(
                chart.encode(),
                "complete",
                lambda output: (
                    brief_contracts.validate_reply(output, chart) == chart.sources
                ),
            )
        case Purpose.DECISION_SUPPORT:
            request = decision_contracts.DecisionRequest(
                identity, chart, tuple(entry.reference() for entry in load_catalog())
            )
            return Trial(
                request.encode(),
                "complete",
                lambda output: (
                    len(decision_contracts.validate_decision_reply(output, request))
                    == 1
                ),
            )
        case Purpose.ANALYST:
            start = EPOCH + timedelta(days=index)
            query = parse_descriptor(
                json.dumps(
                    descriptor(
                        metric_id=("no_show", "waiting_time")[index % 2],
                        start=start,
                        end=start + timedelta(days=1),
                        tz="America/Sao_Paulo",
                    )
                )
            )
            analyst = analyst_contracts.AnalystRequest(query, "1" * 64)
            return Trial(
                analyst.encode(),
                "complete",
                lambda output: (
                    analyst_contracts.validate_reply(output, analyst) == query
                ),
            )
        case _:
            raise EvaluationInputError


def _administrative_trial(
    index: int, identity: UUID, purpose: Purpose, provider: str, model: str
) -> Trial:
    if purpose == Purpose.EXTRACTION:
        return _document(provider, model)
    if purpose == Purpose.CARE_COORDINATION:
        return _care(index, identity)
    return _revenue(index)
