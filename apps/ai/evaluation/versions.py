"""Pin the model, prompt, retrieval and output contract to one evaluated revision."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import rfc8785

from apps.ai.contracts import Purpose
from apps.ai.prompt_registry import load_prompt

if TYPE_CHECKING:
    from apps.ai.models import ModelConfiguration

ROOT = Path(__file__).resolve().parents[3]


def _source_hash(paths: tuple[str, ...]) -> str:
    digest = sha256()
    for name in paths:
        content = (ROOT / name).read_bytes()
        digest.update(name.encode() + b"\0" + len(content).to_bytes(8) + content)
    return digest.hexdigest()


def source_versions(purpose: Purpose) -> dict[str, str]:
    """Invalidate old approval whenever an admitted source contract changes."""
    return {
        "prompt": load_prompt(purpose)[1],
        "retrieval": _source_hash(
            (
                "apps/ai/context.py",
                "apps/core/chart_sources.py",
                "apps/ehr/chart_sources.py",
                "apps/orders/chart_sources.py",
                "apps/orders/queries.py",
                "apps/workflows/chart_sources.py",
                "apps/ai/reference_catalog.py",
                "apps/ai/fixtures/reference-catalog-v1.json",
            )
        ),
        "schema": _source_hash(
            (
                "apps/ai/output_validation.py",
                "apps/ai/adapters.py",
                "apps/ai/contracts.py",
                "apps/ai/fixtures/v1.json",
                "apps/ai/fixtures/document_extraction_v1.json",
                "apps/ai/brief_contracts.py",
                "apps/ai/decision_contracts.py",
                "apps/ai/extraction_contracts.py",
                "apps/scribe/draft_contracts.py",
                "apps/ai/evaluation/corpus.schema.json",
                "docs/ai/brief-v1.json",
                "docs/ai/decision-support-v1.json",
                "docs/ai/action-payload-v1.json",
            )
        ),
    }


def candidate_versions(configuration: ModelConfiguration) -> dict[str, str | int]:
    """Include price, jurisdiction and version identity in the immutable digest."""
    terms = {
        name: getattr(configuration, name)
        for name in (
            "purpose",
            "provider",
            "model_id",
            "region",
            "jurisdiction_class",
            "endpoint",
            "retention_terms",
            "training_terms",
            "input_micros",
            "output_micros",
            "cached_micros",
            "audio_micros",
            "version",
            "route",
            "timeout_seconds",
        )
    }
    return {
        "model": sha256(rfc8785.dumps(terms)).hexdigest(),
        "version": configuration.version,
        **source_versions(Purpose(configuration.purpose)),
    }
