"""Closed fixture/metric boundaries and the CI-independent static audio contract."""

from __future__ import annotations

import json
import subprocess

import pytest
from apps.ai import adapters
from apps.ai.adapters import SyntheticProvider
from apps.ai.contracts import ProviderRequest, ProviderResult, Usage
from apps.ai.evaluation.corpus import (
    CAPABILITIES,
    case_audio,
    load_corpus,
    parse_manifest,
)
from apps.ai.evaluation.metrics import (
    cluster_bootstrap,
    cohens_kappa,
    wilson_interval,
    word_errors,
)
from apps.ai.evaluation.protocol import (
    ENGINEERING_THRESHOLDS,
    EvaluationInputError,
    threshold_document,
    validate_labels,
)
from apps.ai.evaluation.runner import evaluate

from ai.evaluation_support import no_errors


@pytest.mark.parametrize(
    ("reference", "actual", "counts"),
    [
        ("a a b", "a b", (0, 1, 0)),
        ("a b", "a a b", (0, 0, 1)),
        ("a", "b", (1, 0, 0)),
        ("a", "", (0, 1, 0)),
        ("", "", (0, 0, 0)),
    ],
)
def test_alignment_counts_repetitions_and_missing_audio(
    reference: str, actual: str, counts: tuple[int, int, int]
) -> None:
    result = word_errors(reference, actual)
    assert (
        tuple(result[key] for key in ("substitutions", "deletions", "insertions"))
        == counts
    )


def test_invalid_statistical_inputs_fail_instead_of_inventing_denominators() -> None:
    for successes, total in ((1, 0), (-1, 3), (True, 1)):
        with pytest.raises(EvaluationInputError):
            wilson_interval(successes, total)
    with pytest.raises(EvaluationInputError):
        word_errors("x " * 4097, "")
    with pytest.raises(EvaluationInputError):
        cohens_kappa(["a"], [])
    assert cohens_kappa([], []) is None
    assert cohens_kappa(["a", "b"], ["b", "a"]) == -1
    with pytest.raises(EvaluationInputError):
        cluster_bootstrap({"cluster": []}, seed=47, iterations=100)
    with pytest.raises(EvaluationInputError):
        cluster_bootstrap({"cluster": [0]}, seed=47, iterations=1)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("minimum_cases", 0),
        ("minimum_cases", 1001),
        ("minimum_encounters", 1),
        ("maximum_critical_errors", 1),
        ("maximum_error_rate_ppm", -1),
        ("minimum_citation_support_ppm", 1_000_001),
        ("minimum_action_correctness_ppm", -1),
        ("bootstrap_seed", -1),
        ("bootstrap_iterations", 99),
        ("minimum_cases", True),
    ],
)
def test_thresholds_have_closed_integer_domains(key: str, value: int) -> None:
    with pytest.raises(EvaluationInputError):
        threshold_document(dict(ENGINEERING_THRESHOLDS, **{key: value}))


def test_rating_schema_cannot_smuggle_text_or_bool_counts() -> None:
    for labels in (
        {},
        dict(no_errors(), extra="synthetic"),
        dict(no_errors(), negation=True),
        dict(no_errors(), correction_ms=3_600_001),
    ):
        with pytest.raises(EvaluationInputError):
            validate_labels(labels)
    assert validate_labels(no_errors()) == no_errors()


@pytest.mark.parametrize(
    "mutation",
    [
        "real",
        "provenance",
        "capabilities",
        "duplicate",
        "adversarial",
        "clusters",
        "extra",
    ],
)
def test_corpus_refuses_unlawful_duplicate_or_unstratified_manifests(
    mutation: str,
) -> None:
    data = json.loads(json.dumps(load_corpus("engineering-v1").manifest))
    if mutation == "real":
        data["data_class"] = "real_world"
    elif mutation == "provenance":
        data["lawful_provenance"] = ""
    elif mutation == "capabilities":
        data["capabilities"].reverse()
    elif mutation == "duplicate":
        data["cases"][0]["id"] = data["cases"][1]["id"]
    elif mutation == "adversarial":
        data["cases"][0]["adversarial"] = "unknown"
    elif mutation == "clusters":
        for case in data["cases"]:
            case["encounter"] = "synthetic-encounter-01"
    else:
        data["extra"] = "not-in-schema"
    with pytest.raises(EvaluationInputError):
        parse_manifest(json.dumps(data).encode())


def test_registry_refuses_path_selection_and_malformed_json() -> None:
    with pytest.raises(EvaluationInputError):
        load_corpus("../engineering-v1")
    with pytest.raises(EvaluationInputError):
        parse_manifest(b"{")
    with pytest.raises(EvaluationInputError):
        evaluate(
            capability="AI-99",
            corpus_version="engineering-v1",
            provider_key="synthetic",
            model_id="synthetic-v1",
        )


def test_ci_reads_static_hashed_audio_without_any_tts_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    corpus = load_corpus("engineering-v1")
    decoded = [case_audio(case) for case in corpus.cases]
    assert len(decoded) == 12
    assert sum(map(len, decoded)) == 118927
    assert all(audio.startswith(b"OggS") for audio in decoded)
    for capability in CAPABILITIES:
        report = evaluate(
            capability=capability,
            corpus_version="engineering-v1",
            provider_key="synthetic",
            model_id="synthetic-v1",
        )
        assert report["automatic_failures"] == 0


def test_malformed_model_output_is_failed_by_the_shared_gateway_not_a_model_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedOutput(SyntheticProvider):
        def complete(self, *, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                '{"diagnosis":"synthetic unsupported claim"}', Usage()
            )

    monkeypatch.setitem(adapters._PROVIDERS, "synthetic", UnsupportedOutput())
    report = evaluate(
        capability="AI-08",
        corpus_version="engineering-v1",
        provider_key="synthetic",
        model_id="synthetic-v1",
    )
    assert report["automatic_failures"] == 12
    assert report["human_review_complete"] is False
