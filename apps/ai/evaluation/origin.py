"""Capture the immutable run's exact instructions before leaving its transaction."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from apps.ai.contracts import Purpose

if TYPE_CHECKING:
    from apps.ai.models import EvaluationProtocol, EvaluationRun

DOMAIN_PROTOCOL = Path(__file__).with_name("domain-protocol-v2.json")


def domain_protocol_hash() -> str:
    """Identify the preregistered input/expected-outcome contract."""
    return sha256(DOMAIN_PROTOCOL.read_bytes()).hexdigest()


PURPOSE_CAPABILITIES = {
    Purpose.SCRIBE_ASR.value: "AI-01",
    Purpose.SCRIBE_DRAFT.value: "AI-01",
    Purpose.BRIEF.value: "AI-02",
    Purpose.EXTRACTION.value: "AI-03",
    Purpose.FRONT_DESK.value: "AI-04",
    Purpose.CARE_COORDINATION.value: "AI-05",
    Purpose.REVENUE_CYCLE.value: "AI-06",
    Purpose.ANALYST.value: "AI-07",
    Purpose.DECISION_SUPPORT.value: "AI-08",
}


def protocol_identity(protocol: EvaluationProtocol) -> str:
    """Bind the local version, corpus, rubric and preregistered scoring semantics."""
    terms = (
        str(protocol.pk),
        str(protocol.version),
        protocol.capability,
        protocol.thresholds_sha256,
        protocol.corpus_version,
        protocol.corpus_sha256,
        protocol.domain_protocol_sha256,
    )
    return sha256("\n".join(terms).encode()).hexdigest()


def run_origin(run: EvaluationRun) -> dict[str, object]:
    """Do not accept independent run, purpose, provider or protocol selectors."""
    return {
        "run_id": str(run.pk),
        "stage": run.stage,
        "configuration_id": str(run.configuration_id),
        "configuration_version": run.configuration_version,
        "purpose": run.configuration.purpose,
        "provider": run.configuration.provider,
        "model_id": run.configuration.model_id,
        "versions": run.versions,
        "protocol_id": str(run.protocol_id),
        "protocol_version": run.protocol.version,
        "protocol_sha256": protocol_identity(run.protocol),
        "domain_protocol_sha256": domain_protocol_hash(),
        "thresholds_sha256": run.protocol.thresholds_sha256,
        "corpus_sha256": run.protocol.corpus_sha256,
    }
