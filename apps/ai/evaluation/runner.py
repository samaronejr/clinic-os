"""An offline, deterministic runner through the unchanged shared output gateway."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from apps.ai.adapters import provider_for
from apps.ai.brief_contracts import ChartRequest
from apps.ai.contracts import GatewayInputError, ProviderRequest, Purpose, Usage
from apps.ai.decision_contracts import DecisionRequest
from apps.ai.evaluation.corpus import CAPABILITIES, case_audio, load_corpus
from apps.ai.evaluation.metrics import word_errors
from apps.ai.evaluation.protocol import EvaluationInputError
from apps.ai.output_validation import call_provider
from apps.ai.prompt_registry import load_prompt
from apps.ai.reference_catalog import load_catalog
from apps.core.chart_sources import ChartSource

if TYPE_CHECKING:
    from apps.ai.contracts import Method
    from apps.ai.evaluation.corpus import Case


def _request(
    case: Case, capability: str, purpose: Purpose | None
) -> tuple[str, Method]:
    if capability == "AI-01" and purpose != Purpose.SCRIBE_DRAFT:
        return json.dumps(
            {
                "schema": "synthetic-evaluation-audio-v1",
                "encoding": "ogg-opus",
                "audio_base64": base64.b64encode(case_audio(case)).decode(),
            }
        ), "transcribe"
    if capability in {"AI-02", "AI-08"}:
        identity = uuid5(NAMESPACE_URL, "clinic-os-evaluation:" + case["id"])
        source = ChartSource(
            "observations",
            "observation",
            identity,
            1,
            datetime(2031, 1, 1, tzinfo=UTC),
            case["script"],
        )
        chart = ChartRequest(identity, "1" * 64, "brief", "", (source.reference(),))
        if capability == "AI-08":
            return (
                DecisionRequest(
                    identity,
                    chart,
                    tuple(entry.reference() for entry in load_catalog()),
                ).encode(),
                "complete",
            )
        return chart.encode(), "complete"
    return case["script"], "extract" if capability == "AI-03" else "complete"


def evaluate(
    *,
    capability: str,
    corpus_version: str,
    provider_key: str,
    model_id: str,
    purpose: Purpose | None = None,
) -> dict[str, object]:
    """Score fixtures outside transactions; this path cannot call a tool or real AI.

    These are adapter/contract experiments, not the clinical workflow or a claim
    that the fake transcribed the speech or understood the synthetic scenario.
    Semantic quality is deliberately left to independent structured human review.
    """
    if capability not in CAPABILITIES:
        raise EvaluationInputError
    corpus = load_corpus(corpus_version)
    provider = provider_for(provider_key)
    if provider_key not in {"synthetic", "recorded_fixture"} or provider.real_enabled:
        raise EvaluationInputError
    endpoint = (
        "https://ai.synthetic.invalid/v1/inference"
        if provider_key == "synthetic"
        else "https://recorded.synthetic.invalid/v1/inference"
    )
    cases: list[dict[str, object]] = []
    for case in corpus.cases:
        content, method = _request(case, capability, purpose)
        try:
            result = call_provider(
                provider,
                request=ProviderRequest(
                    model_id,
                    endpoint,
                    load_prompt(purpose)[0]
                    if purpose is not None
                    else "Synthetic evaluation only. No tools or side effects.",
                    content,
                    Usage(tokens_in=10000, tokens_out=10000, audio_seconds=60),
                    30,
                ),
                method=method,
            )
            output = result.content
            if method == "extract":
                output = json.loads(output)["text"]
            valid = True
        except GatewayInputError:
            output, valid = "", False
        cases.append(
            {
                **case,
                "output": output,
                "schema_valid": valid,
                "word_errors": word_errors(case["script"], output),
            }
        )
    return {
        "schema_version": 1,
        "capability": capability,
        "corpus_version": corpus_version,
        "corpus_sha256": corpus.sha256,
        "data_class": "synthetic",
        "real_world_performance": False,
        "human_review_complete": False,
        "automatic_failures": sum(not bool(case["schema_valid"]) for case in cases),
        "tool_calls": 0,
        "cases": cases,
    }
