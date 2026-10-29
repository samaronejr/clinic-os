"""Deterministic descriptive statistics; zero observed errors is not safety proof."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from math import floor, sqrt
from typing import TYPE_CHECKING, TypedDict

from apps.ai.evaluation.protocol import (
    MAX_BOOTSTRAP_ITERATIONS,
    MIN_BOOTSTRAP_ITERATIONS,
    EvaluationInputError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_WORDS = 4096


class WordErrors(TypedDict):
    """Retain counts even when an empty reference has no defined rate."""

    substitutions: int
    deletions: int
    insertions: int
    errors: int
    reference_words: int
    rate: float | None


def word_errors(reference: str, hypothesis: str) -> WordErrors:
    """Use case-sensitive whitespace tokens and unit-cost Levenshtein alignment."""
    expected, actual = reference.split(), hypothesis.split()
    if max(len(expected), len(actual)) > MAX_WORDS:
        raise EvaluationInputError
    # Each cell retains (distance, substitutions, deletions, insertions).
    previous = [(index, 0, 0, index) for index in range(len(actual) + 1)]
    for row, token in enumerate(expected, 1):
        current = [(row, 0, row, 0)]
        for column, observed in enumerate(actual, 1):
            distance, substitutions, deletions, insertions = previous[column - 1]
            substitution = (
                distance + (token != observed),
                substitutions + (token != observed),
                deletions,
                insertions,
            )
            distance, substitutions, deletions, insertions = previous[column]
            deletion = (distance + 1, substitutions, deletions + 1, insertions)
            distance, substitutions, deletions, insertions = current[-1]
            insertion = (distance + 1, substitutions, deletions, insertions + 1)
            current.append(min((substitution, deletion, insertion), key=lambda x: x[0]))
        previous = current
    errors, substitutions, deletions, insertions = previous[-1]
    return {
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "errors": errors,
        "reference_words": len(expected),
        "rate": errors / len(expected) if expected else None,
    }


def wilson_interval(successes: int, total: int) -> tuple[float | None, float | None]:
    """Return a two-sided 95 percent Wilson score interval, not a normal Wald CI."""
    if (
        type(successes) is not int
        or type(total) is not int
        or not 0 <= successes <= total
    ):
        raise EvaluationInputError
    if total == 0:
        return None, None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    radius = (
        z
        * sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, centre - radius), min(1.0, centre + radius)


def cohens_kappa(first: Sequence[str], second: Sequence[str]) -> float | None:
    """Report undefined agreement when empty or when chance agreement is one."""
    if len(first) != len(second):
        raise EvaluationInputError
    if not first:
        return None
    total = len(first)
    one, two = Counter(first), Counter(second)
    observed = sum(a == b for a, b in zip(first, second, strict=True)) / total
    chance = (
        sum(one[label] * two[label] for label in one.keys() | two.keys()) / total**2
    )
    return (observed - chance) / (1 - chance) if chance < 1 else None


def cluster_bootstrap(
    clusters: Mapping[str, Sequence[float]], *, seed: int, iterations: int
) -> tuple[float | None, float | None]:
    """Resample encounters, with replacement, retaining every observation in each.

    Counter-based SHA-256 draws use rejection sampling, not platform RNG state.
    A draw selects a whole cluster, never an independently resampled case.
    Percentiles use linear interpolation at (n - 1) * p.
    """
    if (
        type(seed) is not int
        or not MIN_BOOTSTRAP_ITERATIONS <= iterations <= MAX_BOOTSTRAP_ITERATIONS
    ):
        raise EvaluationInputError
    if not clusters:
        return None, None
    ordered = [tuple(clusters[key]) for key in sorted(clusters)]
    if any(not values for values in ordered):
        raise EvaluationInputError
    count = len(ordered)
    modulus = 2**256 - 2**256 % count
    counter = 0
    draws: list[float] = []
    for _ in range(iterations):
        numerator = 0.0
        denominator = 0
        for _ in range(count):
            while True:
                value = int.from_bytes(sha256(f"{seed}:{counter}".encode()).digest())
                counter += 1
                if value < modulus:
                    break
            values = ordered[value % count]
            numerator += sum(values)
            denominator += len(values)
        draws.append(numerator / denominator)
    draws.sort()

    def percentile(probability: float) -> float:
        position = (iterations - 1) * probability
        index = floor(position)
        return draws[index] + (position - index) * (draws[index + 1] - draws[index])

    return percentile(0.025), percentile(0.975)
