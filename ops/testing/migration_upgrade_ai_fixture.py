"""Declared inputs to the synthetic AI invocation histories."""

from typing import Final

HISTORIES: Final = (
    ("AI-04", "front_desk"),
    ("AI-05", "care_coordination"),
    ("AI-06", "revenue_cycle"),
    ("AI-01", "scribe_asr"),
    ("AI-01", "scribe_draft"),
    ("AI-03", "extraction"),
    ("AI-07", "analyst"),
)
STATUSES: Final = ("reserved", "succeeded")
OWNER_PURPOSES: Final = frozenset({"front_desk", "revenue_cycle", "analyst"})
UNBOUND_PURPOSES: Final = frozenset({"front_desk", "revenue_cycle"})
METHODS: Final = {"scribe_asr": "transcribe", "extraction": "extract"}
ARTIFACT_PURPOSE: Final = "ai.artifact.content.v1"
INPUT_TEMPLATE: Final = "Synthetic upgrade input [{purpose}/{status}]."
OUTPUT_TEMPLATE: Final = "Synthetic retained output [{purpose}/{status}]."
DIGEST: Final = "a" * 64
INVOCATION_LIMIT: Final = 10
RESERVED_MICROS: Final = 100

CONFIGURATION: Final = {
    "provider": "synthetic",
    "model_id": "upgrade-synthetic",
    "region": "br-synthetic",
    "jurisdiction_class": "BR",
    "endpoint": "https://ai.synthetic.invalid/",
    "retention_terms": "synthetic",
    "training_terms": "synthetic",
    "version": 1,
    "route": "primary",
    "state": "active",
    "approval_ref": "synthetic",
    "prompt_sha256": DIGEST,
    "retrieval_sha256": DIGEST,
    "schema_sha256": DIGEST,
}
