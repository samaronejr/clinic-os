"""Offline capability trials; only an observation produced for a run can be stored."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from hmac import compare_digest
from typing import TYPE_CHECKING

from django.core import signing
from django.db import connections

from apps.ai.adapters import provider_for
from apps.ai.contracts import (
    GatewayInputError,
    ProviderRequest,
    ProviderUnavailableError,
    Purpose,
    Usage,
)
from apps.ai.evaluation.corpus import load_corpus
from apps.ai.evaluation.metrics import word_errors
from apps.ai.evaluation.origin import (
    PURPOSE_CAPABILITIES,
    domain_protocol_hash,
    run_origin,
)
from apps.ai.evaluation.protocol import EvaluationInputError
from apps.ai.evaluation.trials import front_desk_case, transcript, trial
from apps.ai.evaluation.versions import candidate_versions
from apps.ai.extraction_contracts import ExtractionInputError
from apps.ai.output_validation import call_provider
from apps.ai.prompt_registry import load_prompt
from apps.scribe.draft_contracts import DraftInputError
from apps.scribe.transcript_contracts import TranscriptError

if TYPE_CHECKING:
    from apps.ai.evaluation.corpus import Case
    from apps.ai.models import EvaluationRun


def _receipt(samples: dict[str, object]) -> str:
    digest = sha256(
        json.dumps(
            samples, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    return signing.Signer(salt="ai.evaluation.produced-samples.v2").sign(digest)


def verify_receipt(results: dict[str, object]) -> bool:
    """Authenticate the *whole* producer output before checking its run binding."""
    unsigned = results.copy()
    receipt = unsigned.pop("receipt", None)
    if not isinstance(receipt, str):
        return False
    try:
        return compare_digest(receipt, _receipt(unsigned))
    except (ValueError, TypeError):
        return False


def score_run(run: EvaluationRun) -> dict[str, object]:
    """Score only the terms of this run, outside its authorizing transaction."""
    if (
        run.scored_at
        or run.status != "pending"
        or run.protocol.domain_protocol_sha256 != domain_protocol_hash()
        or candidate_versions(run.configuration) != run.versions
    ):
        raise EvaluationInputError
    results = _evaluate(
        purpose=Purpose(run.configuration.purpose),
        corpus_version=run.protocol.corpus_version,
        terms=ProviderRequest(
            run.configuration.model_id,
            run.configuration.endpoint,
            load_prompt(Purpose(run.configuration.purpose))[0],
            "",
            Usage(tokens_in=10000, tokens_out=10000, audio_seconds=60),
            run.configuration.timeout_seconds,
        ),
        provider_key=run.configuration.provider,
        origin=run_origin(run),
    )
    results["receipt"] = _receipt(results)
    return results


def evaluate(
    *,
    capability: str,
    corpus_version: str,
    provider_key: str,
    model_id: str,
    purpose: Purpose | None = None,
) -> dict[str, object]:
    """Repeatable, detached engineering replay; this output cannot be stored."""
    selected = purpose or next(
        (p for p in Purpose if PURPOSE_CAPABILITIES[p.value] == capability), None
    )
    if selected is None or PURPOSE_CAPABILITIES[selected.value] != capability:
        raise EvaluationInputError
    endpoint = "ai" if provider_key == "synthetic" else "recorded"
    terms = ProviderRequest(
        model_id,
        f"https://{endpoint}.synthetic.invalid/v1/inference",
        load_prompt(selected)[0],
        "",
        Usage(tokens_in=10000, tokens_out=10000, audio_seconds=60),
        30,
    )
    return _evaluate(
        purpose=selected,
        corpus_version=corpus_version,
        terms=terms,
        provider_key=provider_key,
        origin=None,
    )


def _observe(
    case: Case, index: int, purpose: Purpose, provider_key: str, terms: ProviderRequest
) -> tuple[str, bool]:
    if purpose == Purpose.FRONT_DESK:
        return front_desk_case(index, case)
    experiment = trial(case, index, purpose, provider_key, terms.model_id)
    result = call_provider(
        provider_for(provider_key),
        request=replace(terms, content=experiment.content),
        method=experiment.method,
    )
    output = (
        json.loads(result.content)["text"]
        if experiment.method == "extract"
        else result.content
    )
    return output, experiment.observe(output)


def _evaluate(
    *,
    purpose: Purpose,
    corpus_version: str,
    terms: ProviderRequest,
    provider_key: str,
    origin: dict[str, object] | None,
) -> dict[str, object]:
    if (
        provider_key not in {"synthetic", "recorded_fixture"}
        or provider_for(provider_key).real_enabled
        or any(
            db.in_atomic_block or (db.connection is not None and not db.autocommit)
            for db in connections.all(initialized_only=True)
        )
    ):
        raise EvaluationInputError
    corpus = load_corpus(corpus_version)
    cases: list[dict[str, object]] = []
    for index, case in enumerate(corpus.cases):
        try:
            output, valid = _observe(case, index, purpose, provider_key, terms)
        except (
            GatewayInputError,
            ProviderUnavailableError,
            DraftInputError,
            ExtractionInputError,
            TranscriptError,
        ):
            output, valid = "", False
        cases.append(
            {
                **case,
                "output": output,
                "schema_valid": valid,
                "word_errors": (
                    word_errors(case["script"], transcript(output) if valid else "")
                    if purpose == Purpose.SCRIBE_ASR
                    else word_errors("", "")
                ),
                "origin": origin,
            }
        )
    return {
        "schema_version": 2,
        "capability": PURPOSE_CAPABILITIES[purpose.value],
        "corpus_version": corpus_version,
        "corpus_sha256": corpus.sha256,
        "domain_protocol_sha256": domain_protocol_hash(),
        "origin": origin,
        "data_class": "synthetic",
        "real_world_performance": False,
        "human_review_complete": False,
        "automatic_failures": sum(not case["schema_valid"] for case in cases),
        "tool_calls": 0,
        "cases": cases,
    }
