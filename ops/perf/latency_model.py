"""Seeded latency models for providers the harness does not call (virtual time).

Parameters are assumptions, labeled as such in the report; the outputs are
never measurements and never count as a target met (SC-16: zero real-provider
calls, deterministic fake model).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random


@dataclass(frozen=True, slots=True)
class ModelParams:
    """Assumed fake-model and ASR timing; every value is an assumption."""

    slice_ms: float = 2000.0
    asr_median_ms: float = 650.0
    asr_sigma: float = 0.35
    push_ms: float = 40.0
    prefill_tokens: int = 6000
    prefill_tokens_per_s: float = 3000.0
    decode_tokens: int = 900
    decode_tokens_per_s: float = 45.0
    jitter_sigma: float = 0.15

    def as_dict(self) -> dict[str, float]:
        """Return the parameters for the report notes."""
        return {key: float(value) for key, value in asdict(self).items()}


def transcript_partial_ms(
    *, params: ModelParams, ingest_ms: float, rng: random.Random
) -> float:
    """Model a partial transcript: wait inside a slice, ingest, ASR, push."""
    wait = rng.uniform(0.0, params.slice_ms)
    asr = params.asr_median_ms * rng.lognormvariate(0.0, params.asr_sigma)
    return wait + ingest_ms + asr + params.push_ms


def draft_generation_ms(
    *, params: ModelParams, persist_ms: float, rng: random.Random
) -> float:
    """Model the final draft after stop for the 30-minute reference encounter."""
    prefill = params.prefill_tokens / params.prefill_tokens_per_s
    decode = params.decode_tokens / params.decode_tokens_per_s
    jitter = rng.lognormvariate(0.0, params.jitter_sigma)
    return (prefill + decode) * 1000.0 * jitter + persist_ms
