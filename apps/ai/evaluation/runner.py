"""Offline capability trials; only an observation produced for a run can be stored."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from typing import TYPE_CHECKING

from django.db import connections, transaction

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
    observation_receipt,
    packet_bytes,
    run_origin,
)
from apps.ai.evaluation.protocol import EvaluationInputError
from apps.ai.evaluation.services import authorized_run
from apps.ai.evaluation.trials import front_desk_case, transcript, trial
from apps.ai.evaluation.versions import candidate_versions
from apps.ai.extraction_contracts import ExtractionInputError
from apps.ai.models import EvaluationRun
from apps.ai.output_validation import call_provider
from apps.ai.prompt_registry import load_prompt
from apps.identity.management.base import assert_owner_database_role
from apps.identity.management.context import scoped_lifecycle_gucs
from apps.scribe.draft_contracts import DraftInputError
from apps.scribe.transcript_contracts import TranscriptError

if TYPE_CHECKING:
    from apps.ai.evaluation.corpus import Case


@dataclass
class _ScoringInputs:
    origin: dict[str, object]
    digests: list[str] = field(default_factory=list)

    def record(self, value: dict[str, object]) -> None:
        self.digests.append(sha256(packet_bytes(value)).hexdigest())


def score_run(run: EvaluationRun) -> dict[str, object]:
    """Score only the terms of this run, outside its authorizing transaction."""
    assert_owner_database_role()
    with (
        transaction.atomic(),
        scoped_lifecycle_gucs(run.organization_id, run.created_by_id),
    ):
        run = authorized_run(clinic_id=run.clinic_id, run_id=run.pk)
    if (
        run.scored_at
        or run.status != "pending"
        or run.protocol.domain_protocol_sha256 != domain_protocol_hash()
        or candidate_versions(run.configuration) != run.versions
    ):
        raise EvaluationInputError
    bound = _ScoringInputs(run_origin(run))
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
        bound=bound,
    )
    results["scorer_id"] = run.versions["schema"]
    results["input_sha256"] = sha256(
        packet_bytes({"origin": bound.origin, "inputs": bound.digests})
    ).hexdigest()
    results["receipt"] = observation_receipt(results)
    # Register the *complete, already-signed* score, not a caller-supplied
    # digest. A runtime connection cannot register or replace this commitment.
    commitment = sha256(packet_bytes(results)).digest()
    with (
        transaction.atomic(),
        scoped_lifecycle_gucs(run.organization_id, run.created_by_id),
    ):
        locked = EvaluationRun.objects.select_for_update().get(pk=run.pk)
        if locked.status != "pending" or locked.scored_at is not None:
            raise EvaluationInputError
        if locked.producer_commitment is None:
            locked.producer_commitment = commitment
            locked.save(update_fields=("producer_commitment",))
        elif bytes(locked.producer_commitment) != commitment:
            raise EvaluationInputError
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
        bound=None,
    )


def _observe(
    indexed_case: tuple[int, Case],
    purpose: Purpose,
    provider_key: str,
    terms: ProviderRequest,
    bound: _ScoringInputs | None,
) -> tuple[str, bool]:
    index, case = indexed_case
    if purpose == Purpose.FRONT_DESK:
        if bound is not None:
            bound.record(
                {"case": case, "index": index, "protocol": domain_protocol_hash()}
            )
        return front_desk_case(index, case)
    experiment = trial(case, index, purpose, provider_key, terms.model_id)
    request = replace(terms, content=experiment.content)
    if bound is not None:
        bound.record(
            {"id": case["id"], "method": experiment.method, "request": asdict(request)}
        )
    result = call_provider(
        provider_for(provider_key),
        request=request,
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
    bound: _ScoringInputs | None,
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
    origin = None if bound is None else bound.origin
    cases: list[dict[str, object]] = []
    for index, case in enumerate(corpus.cases):
        try:
            output, valid = _observe((index, case), purpose, provider_key, terms, bound)
        except (
            GatewayInputError,
            ProviderUnavailableError,
            DraftInputError,
            ExtractionInputError,
            TranscriptError,
        ):
            output, valid = "", False
            if bound is not None and len(bound.digests) == index:
                bound.record({"unroutable_case": case})
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
