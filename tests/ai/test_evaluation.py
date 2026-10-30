"""Evaluation acceptance: measured fake runs cannot authorize unevaluated changes."""

from __future__ import annotations

from importlib import import_module

import pytest
from apps.ai.contracts import Price, Purpose
from apps.ai.owner import ModelTerms, propose_model, transition_model
from django.db import DatabaseError, transaction


def test_fake_evaluation_is_deterministic_for_every_capability() -> None:
    runner = import_module("apps.ai.evaluation.runner")
    corpus = import_module("apps.ai.evaluation.corpus")
    for capability in corpus.CAPABILITIES:
        first = runner.evaluate(
            capability=capability,
            corpus_version="engineering-v1",
            provider_key="synthetic",
            model_id="synthetic-v1",
        )
        second = runner.evaluate(
            capability=capability,
            corpus_version="engineering-v1",
            provider_key="synthetic",
            model_id="synthetic-v1",
        )
        assert first == second
        assert first["data_class"] == "synthetic"
        assert first["real_world_performance"] is False
        assert first["cases"]
        assert first["human_review_complete"] is False
        assert first["tool_calls"] == 0


def test_error_metrics_preserve_insertions_and_unknown_denominators() -> None:
    metrics = import_module("apps.ai.evaluation.metrics")
    result = metrics.word_errors("a b c", "a c d")
    assert result["errors"] == 2
    assert result["reference_words"] == 3
    assert result["rate"] == pytest.approx(2 / 3)
    assert metrics.word_errors("", "extra")["rate"] is None
    assert metrics.word_errors("", "extra")["insertions"] == 1
    assert metrics.wilson_interval(0, 0) == (None, None)
    low, high = metrics.wilson_interval(0, 10)
    assert low == pytest.approx(0, abs=1e-12)
    assert 0 < high < 1
    assert metrics.cohens_kappa(["a", "b"], ["a", "b"]) == 1
    assert metrics.cohens_kappa(["a"], ["a"]) is None


def test_bootstrap_resamples_whole_encounters_reproducibly() -> None:
    metrics = import_module("apps.ai.evaluation.metrics")
    clusters = {"encounter-a": (0, 0, 0), "encounter-b": (1,)}
    first = metrics.cluster_bootstrap(clusters, seed=47, iterations=2000)
    assert first == metrics.cluster_bootstrap(clusters, seed=47, iterations=2000)
    assert first == (0, 1)
    assert metrics.cluster_bootstrap({}, seed=47, iterations=2000) == (None, None)


@pytest.mark.django_db(transaction=True)
def test_configuration_activation_without_evaluation_is_refused() -> None:
    candidate = propose_model(
        terms=ModelTerms(
            purpose=Purpose.FRONT_DESK,
            provider="synthetic",
            model_id="synthetic-v1",
            region="br-synthetic",
            retention_terms="synthetic-no-retention",
            training_terms="synthetic-no-training",
            price=Price(3, 5, 1, 7),
        )
    )
    transition_model(
        configuration_id=candidate.pk,
        state="approved",
        approval_ref="synthetic-terms-review",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        transition_model(configuration_id=candidate.pk, state="active")
