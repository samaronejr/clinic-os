"""Offline candidate comparison; neither a clinical pass nor provider approval."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from importlib import import_module
from math import ceil
from pathlib import Path
from time import perf_counter_ns
from typing import TYPE_CHECKING, Final, Literal, cast

from django.db import transaction

from apps.ai.adapters import provider_for
from apps.ai.contracts import (
    REGION_CLASSES,
    GatewayInputError,
    Price,
    ProviderRequest,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Purpose,
    Usage,
)
from apps.ai.evaluation.corpus import ADVERSARIAL_TAGS, Corpus, load_corpus
from apps.ai.evaluation.metrics import WordErrors, word_errors
from apps.ai.evaluation.protocol import CLINICAL_LABELS, EvaluationInputError
from apps.ai.evaluation.runner import _request
from apps.ai.output_validation import call_provider
from apps.ai.prompt_registry import load_prompt
from apps.identity.management.base import assert_owner_database_role
from apps.providers.lifecycle import VersionInput, propose_version
from apps.providers.models import CapabilityVersion

if TYPE_CHECKING:
    from collections.abc import Sequence

CAPABILITY: Final = {
    Purpose.SCRIBE_ASR: "AI-01",
    Purpose.SCRIBE_DRAFT: "AI-01",
    Purpose.BRIEF: "AI-02",
    Purpose.EXTRACTION: "AI-03",
    Purpose.FRONT_DESK: "AI-04",
    Purpose.ANALYST: "AI-07",
    Purpose.DECISION_SUPPORT: "AI-08",
}
PROTOCOL: Final = {
    "version": "benchmark-v1",
    "corpus": "engineering-v1",
    "jurisdiction": "BR",
    "ranking": ["failed_calls", "word_errors", "cost_micros", "candidate_name"],
    "latency_quantile": "nearest-rank",
    "retries": 0,
    "clinical_review": "pending_two_raters",
    "eligibility": "verified_region_and_retention_same_jurisdiction_no_failed_calls",
}
MAX_CANDIDATES: Final = 16
ENDPOINTS: Final = {
    "synthetic": "https://ai.synthetic.invalid/v1/inference",
    "recorded_fixture": "https://recorded.synthetic.invalid/v1/inference",
}


@dataclass(frozen=True, slots=True)
class Fact:
    """A claim retains its source/date, including when it is unverified."""

    value: str
    verification: Literal["verified", "unverified"]
    source: str
    retrieved: str

    def verified(self) -> bool:
        """Missing or undated terms never become eligible by implication."""
        try:
            retrieved = date.fromisoformat(self.retrieved)
        except ValueError:
            return False
        return bool(
            self.verification == "verified"
            and self.value
            and self.source
            and retrieved <= datetime.now(UTC).date()
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    """Runnable candidates can only identify the two closed offline adapters."""

    name: str
    adapter: str
    model: str
    jurisdiction: str
    region: Fact
    retention: Fact
    price: Price


OFFLINE_CANDIDATES: Final = (
    Candidate(
        "synthetic-primary",
        "synthetic",
        "synthetic-v1",
        "BR",
        Fact("br-synthetic", "verified", "apps/ai/adapters.py", "2026-10-29"),
        Fact(
            "Offline memory only; no network, training or provider storage",
            "verified",
            "apps/ai/adapters.py",
            "2026-10-29",
        ),
        Price(0, 0, 0, 0),
    ),
    Candidate(
        "recorded-fallback",
        "recorded_fixture",
        "fixture-v1",
        "BR",
        Fact("br-synthetic-secondary", "verified", "apps/ai/adapters.py", "2026-10-29"),
        Fact(
            "Static self-authored synthetic recording; no provider storage",
            "verified",
            "apps/ai/fixtures/v1.json",
            "2026-10-29",
        ),
        Price(0, 0, 0, 0),
    ),
)


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Only aggregate counts leave the corpus boundary, never input or output."""

    candidate: Candidate
    eligible: bool
    reasons: tuple[str, ...]
    samples: int
    encounters: int
    wer: WordErrors
    structured_output: dict[str, int | float]
    clinical_error_rates: dict[str, None]
    clinical_review_status: str
    latency_ms: dict[str, float]
    usage_per_visit: dict[str, float]
    cost_per_visit_micros: float
    failures: dict[str, int]


@dataclass(frozen=True, slots=True)
class Selection:
    """No fallback is safer than a route which changes jurisdiction or terms."""

    purpose: str
    primary: str | None
    fallback: str | None
    candidates: tuple[CandidateResult, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Machine-consumed JSON front matter is also valid YAML front matter."""

    schema_version: int
    generated_at: str
    corpus_version: str
    corpus_sha256: str
    protocol: dict[str, object]
    protocol_sha256: str
    data_class: str
    real_world_performance: bool
    external_gates_satisfied: tuple[str, ...]
    provider_leg: str
    adversarial_classes: tuple[str, ...]
    selections: tuple[Selection, ...]


def _ineligible_reasons(
    candidate: Candidate, failures: Counter[str]
) -> tuple[str, ...]:
    reasons = [
        f"{key}_unverified"
        for key, fact in (
            ("region", candidate.region),
            ("retention", candidate.retention),
        )
        if not fact.verified()
    ]
    if (
        candidate.jurisdiction != PROTOCOL["jurisdiction"]
        or REGION_CLASSES.get(candidate.region.value) != PROTOCOL["jurisdiction"]
    ):
        reasons.append("jurisdiction_mismatch")
    if failures:
        reasons.append("failed_calls")
    return tuple(reasons)


def _measure(candidate: Candidate, purpose: Purpose, corpus: Corpus) -> CandidateResult:
    provider = provider_for(candidate.adapter)
    if provider.real_enabled:
        raise EvaluationInputError
    latencies: list[float] = []
    failures: Counter[str] = Counter()
    errors = word_errors("", "")
    units = dict.fromkeys(
        ("tokens_in", "tokens_out", "tokens_cached", "audio_seconds"), 0
    )
    cost = 0
    valid = 0
    for case in corpus.cases:
        content, method = _request(case, CAPABILITY[purpose], purpose)
        request = ProviderRequest(
            candidate.model,
            ENDPOINTS[candidate.adapter],
            load_prompt(purpose)[0],
            content,
            Usage(tokens_in=10000, tokens_out=10000, audio_seconds=60),
            30,
        )
        start = perf_counter_ns()
        output = ""
        try:
            result = call_provider(provider, request=request, method=method)
            output = result.content
            valid += 1
            for key in units:
                units[key] += getattr(result.usage, key)
            cost += candidate.price.cost(result.usage)
        except ProviderTimeoutError:
            failures["timeout"] += 1
        except ProviderUnavailableError:
            failures["unavailable"] += 1
        except GatewayInputError:
            failures["invalid_output"] += 1
        finally:
            latencies.append((perf_counter_ns() - start) / 1_000_000)
        if purpose == Purpose.SCRIBE_ASR:
            measured = word_errors(case["script"], output)
            errors["substitutions"] += measured["substitutions"]
            errors["deletions"] += measured["deletions"]
            errors["insertions"] += measured["insertions"]
            errors["errors"] += measured["errors"]
            errors["reference_words"] += measured["reference_words"]
    errors["rate"] = (
        errors["errors"] / errors["reference_words"]
        if errors["reference_words"]
        else None
    )
    reasons = _ineligible_reasons(candidate, failures)
    latencies.sort()
    samples = len(corpus.cases)
    encounters = len({case["encounter"] for case in corpus.cases})
    return CandidateResult(
        candidate,
        not reasons,
        tuple(reasons),
        samples,
        encounters,
        errors,
        {"valid": valid, "total": samples, "rate": valid / samples},
        dict.fromkeys(CLINICAL_LABELS),
        "pending_two_raters",
        {f"p{q}": latencies[ceil(samples * q / 100) - 1] for q in (50, 95)},
        {key: value / encounters for key, value in units.items()},
        cost / encounters,
        dict(failures),
    )


def benchmark(
    *,
    purposes: Sequence[Purpose] = tuple(Purpose),
    candidates: Sequence[Candidate] = OFFLINE_CANDIDATES,
) -> BenchmarkReport:
    """Run all EV cases per purpose with no retries, network or domain writes."""
    if (
        not purposes
        or len(set(purposes)) != len(purposes)
        or any(purpose not in CAPABILITY for purpose in purposes)
        or not candidates
        or len(candidates) > MAX_CANDIDATES
        or len({candidate.name for candidate in candidates}) != len(candidates)
        or any(
            candidate.adapter not in {"synthetic", "recorded_fixture"}
            for candidate in candidates
        )
    ):
        raise EvaluationInputError
    corpus = load_corpus("engineering-v1")
    protocol = cast("dict[str, object]", json.loads(json.dumps(PROTOCOL)))
    protocol_hash = sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
    selections: list[Selection] = []
    for purpose in purposes:
        results = tuple(
            _measure(candidate, purpose, corpus) for candidate in candidates
        )
        eligible = sorted(
            (result for result in results if result.eligible),
            key=lambda row: (
                sum(row.failures.values()),
                row.wer["errors"],
                row.cost_per_visit_micros,
                row.candidate.name,
            ),
        )
        selections.append(
            Selection(
                purpose.value,
                eligible[0].candidate.name if eligible else None,
                eligible[1].candidate.name if len(eligible) > 1 else None,
                results,
            )
        )
    return BenchmarkReport(
        schema_version=1,
        generated_at=datetime.now(UTC).isoformat(),
        corpus_version="engineering-v1",
        corpus_sha256=corpus.sha256,
        protocol=protocol,
        protocol_sha256=protocol_hash,
        data_class="synthetic",
        real_world_performance=False,
        external_gates_satisfied=(),
        provider_leg="BLOCKED-ON-EG",
        adversarial_classes=tuple(sorted(ADVERSARIAL_TAGS)),
        selections=tuple(selections),
    )


def markdown_report(report: BenchmarkReport) -> str:
    """Export a complete schema with unknown metrics explicitly null, never zero."""
    frontmatter = json.dumps(
        asdict(report), ensure_ascii=True, indent=2, sort_keys=True
    )
    lines = [
        "---",
        frontmatter,
        "---",
        "# Synthetic ASR/LLM benchmark",
        "",
        "Engineering adapter measurements only; not real-world performance.",
        "Clinical error rates are unknown pending blinded two-rater review (todo 47).",
        "Latency is local adapter wall time, not streaming partial or provider SLA.",
        "Cost is zero paid spend; units are measured per encounter, not vendor prices.",
        "Live/provider/sandbox legs: BLOCKED-ON-EG (EG-1/2/3/5/11).",
        "Manual documentation is required when no eligible fallback exists.",
        "",
        "| Purpose | Primary | Fallback |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {row.purpose} | {row.primary or 'none'} | {row.fallback or 'manual'} |"
        for row in report.selections
    )
    lines.extend(("", "| Candidate | Eligibility | Reasons |", "| --- | --- | --- |"))
    for selection in report.selections:
        lines.extend(
            f"| {row.candidate.name} ({selection.purpose}) | "
            f"{'eligible' if row.eligible else 'não elegível'} | "
            f"{', '.join(row.reasons) or 'offline engineering only'} |"
            for row in selection.candidates
        )
    return "\n".join(lines) + "\n"


def parse_report(content: str) -> dict[str, object]:
    """Validate machine-consumed front matter before accepting a report."""
    try:
        prefix, frontmatter, _body = content.split("---\n", 2)
        if prefix:
            raise EvaluationInputError
        parsed = json.loads(frontmatter)
        schema = json.loads(
            Path(__file__).with_name("benchmark.schema.json").read_text()
        )
        import_module("jsonschema").Draft202012Validator(schema).validate(parsed)
    except (ValueError, import_module("jsonschema").ValidationError):
        raise EvaluationInputError from None
    if (
        parsed["protocol"] != PROTOCOL
        or parsed["protocol_sha256"]
        != sha256(json.dumps(PROTOCOL, sort_keys=True).encode()).hexdigest()
        or set(parsed["adversarial_classes"]) != ADVERSARIAL_TAGS
    ):
        raise EvaluationInputError
    return cast("dict[str, object]", parsed)


def record_selections(
    report: BenchmarkReport, *, record_ref: str
) -> tuple[CapabilityVersion, ...]:
    """Append owner-only synthetic selected_in_plan records, never approvals."""
    assert_owner_database_role()
    parse_report(markdown_report(report))
    rows: list[CapabilityVersion] = []
    with transaction.atomic():
        for selection in report.selections:
            for route in ("fallback", "primary"):
                name = getattr(selection, route)
                if name is None:
                    continue
                candidate = next(
                    row.candidate
                    for row in selection.candidates
                    if row.candidate.name == name and row.eligible
                )
                rows.append(
                    propose_version(
                        f"benchmark_{selection.purpose}_{route}",
                        version_input=VersionInput(
                            provider=candidate.adapter,
                            environment="synthetic",
                            api_version=candidate.model,
                            region=candidate.region.value,
                            retention_terms=candidate.retention.value,
                            state=CapabilityVersion.State.SELECTED_IN_PLAN,
                            record_ref=record_ref,
                            description=report.protocol_sha256,
                        ),
                    )
                )
    return tuple(rows)
